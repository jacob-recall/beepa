// Relocated verbatim from hub/site/app.js (PLAN-MASTER-SYNC-IMPL P1.2).
// Shared ES module. Logic unchanged; only import/export + shared-state (S) access added.

import { $, el, sanitize, sanitizeLine } from './el.js';
import { GMSG, IG, IMSG, LI, PLANNED_SOURCES, SOURCES, TW, WA, clearQR, groupsFor, redactMgmtEvent, sendCmd, sendSecretToMgmt, sendStatusRefresh } from './sources.js';
import { S, runtime } from '../state.js';

// ---- console ----
function logConsole(who, text, srcId) {
  const c = $('console');
  if (!c) return;
  const entry = el('div', 'entry');
  const label = who === 'you' ? 'you' : who === 'error' ? 'error' : (srcId || 'bridge');
  entry.appendChild(el('span', 'who' + (who === 'you' ? ' you' : ''), label + '  '));
  entry.appendChild(el('span', who === 'error' ? 'error' : '', text));
  c.appendChild(entry);
  while (c.childElementCount > 200) c.removeChild(c.firstElementChild);
  c.scrollTop = c.scrollHeight;
}

// ---- pasted-session credential cleanup -----------------------------------
// A pasted Instagram/LinkedIn/X session is a bearer credential. It is sent
// through the C-1 mgmt guard, then the leaked message event is redacted in-app
// with the user's own token (redactMgmtEvent). If that auto-redaction fails, we
// do NOT tell the user to go delete it "in Element" — Element is no longer on
// the daily path — but instead offer a one-click IN-APP retry that re-issues the
// redaction. Only if THAT also fails do we point at the opt-in Element escape
// hatch as a last resort. No credential is retained here: the `secret` is nulled
// by the caller; this only carries the room+event id of the message to delete.
const ESCAPE_HINT = 'Bring up the opt-in Element escape hatch ' +
  '(docker compose --profile escape up -d element) and delete your pasted message there.';

// Remove any in-app redaction-retry UI previously appended after `warnEl`
// (called when a submit handler resets its warning area).
function clearRedactRetry(warnEl) {
  const box = warnEl && warnEl.nextElementSibling;
  if (box && box.classList.contains('redact-retry')) box.remove();
}

// Show the in-app "Delete it now" retry for a credential event that was sent but
// whose auto-redaction did not confirm. On success it clears the warning and
// calls onCleared (e.g. to hide the paste UI); on a failed retry it reveals the
// escape-hatch last resort. Reuses a single sibling container so repeated
// failures never stack buttons. All nodes are el()/textContent — no innerHTML.
function showRedactFailure(warnEl, roomId, eventId, onCleared) {
  const host = warnEl && warnEl.parentNode;
  if (!host) return;
  warnEl.textContent = 'Your pasted session was sent but has not yet been deleted from the bridge room.';
  warnEl.classList.remove('hidden');
  let box = warnEl.nextElementSibling;
  if (!box || !box.classList.contains('redact-retry')) {
    box = el('div', 'redact-retry');
    host.insertBefore(box, warnEl.nextSibling);
  }
  box.replaceChildren();
  const btn = el('button', 'primary', 'Delete it now');
  btn.type = 'button';
  btn.style.width = 'auto';
  const last = el('p', 'muted hidden', 'Still not deleted. ' + ESCAPE_HINT);
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await redactMgmtEvent(roomId, eventId);
      warnEl.textContent = 'Deleted. Your pasted session is no longer stored in the bridge room.';
      warnEl.classList.remove('hidden');
      box.remove();
      if (typeof onCleared === 'function') onCleared();
    } catch (e) {
      last.classList.remove('hidden');
      btn.disabled = false;
    }
  });
  box.appendChild(btn);
  box.appendChild(last);
}

function setButtonsDisabled(v) {
  for (const b of document.querySelectorAll('#command-groups button, .bridge-actions button, .startable')) {
    b.disabled = v;                                 // capability-disabled controls are excluded
  }
}
function setLoginFlow(active) {
  S.loginFlowActive = active;
  const btn = $('btn-cancel-login');
  if (btn) btn.classList.toggle('hidden', !active);
  const gmBtn = $('btn-gmsg-cancel-login');
  if (gmBtn) gmBtn.classList.toggle('hidden', !active);
  if (!active) clearQR();
}

// ---- WhatsApp / Google Messages Connections card status ----
let platformRailHook = null;
function setPlatformRailHook(fn) { platformRailHook = typeof fn === 'function' ? fn : null; }
function notifyPlatformRail() { if (platformRailHook) platformRailHook(); }

const PILL_SOURCE = {
  'wa-status': 'whatsapp', 'gmsg-status': 'gmessages', 'ig-status': 'instagram',
  'li-status': 'linkedin', 'tw-status': 'twitter',
};

// #3: a transient per-source UI phase so the connect action gives immediate,
// legible feedback — "Connecting…" while it runs, then "importing conversations"
// after it succeeds — that a mid-flight list-logins refresh won't clobber.
// Phases auto-clear so a pill never sticks: 'connecting' after 30s, 'importing'
// after 60s, each then restoring the real login status via list-logins.
const SOURCE_PILL = { whatsapp: 'wa-status', gmessages: 'gmsg-status', instagram: 'ig-status', linkedin: 'li-status', twitter: 'tw-status' };
const sourcePhase = {};
const sourcePhaseTimer = {};
function renderSourcePill(sourceId) {
  const pill = $(SOURCE_PILL[sourceId] || '');
  if (!pill) return;
  const phase = sourcePhase[sourceId];
  if (phase !== 'connecting' && phase !== 'importing') return;
  pill.classList.remove('ok', 'stale');
  pill.classList.add('busy');
  pill.textContent = phase === 'connecting' ? 'Connecting…' : 'Connected — importing conversations…';
}
function setSourcePhase(sourceId, phase) {
  if (!sourceId) return;
  if (sourcePhaseTimer[sourceId]) { clearTimeout(sourcePhaseTimer[sourceId]); sourcePhaseTimer[sourceId] = null; }
  sourcePhase[sourceId] = (phase === 'connecting' || phase === 'importing') ? phase : undefined;
  if (sourcePhase[sourceId]) {
    renderSourcePill(sourceId);
    sourcePhaseTimer[sourceId] = setTimeout(() => {
      sourcePhase[sourceId] = undefined; sourcePhaseTimer[sourceId] = null;
      sendCmd(sourceId, 'list-logins');            // restore the real login status
    }, phase === 'importing' ? 60000 : 30000);
  } else {
    sendCmd(sourceId, 'list-logins');
  }
}

// A bridge login is only healthily connected when the bridge reports CONNECTED.
// BAD_CREDENTIALS / LOGGED_OUT mean the stored session is dead and the user must
// re-pair (reset the sync) — surfaced distinctly so a stale login never reads as
// "Connected". Other non-connected states (CONNECTING / TRANSIENT_DISCONNECT /
// UNKNOWN_ERROR) are transient and shown as "reconnecting", not a reset prompt.
const NEEDS_RESET = new Set(['BAD_CREDENTIALS', 'LOGGED_OUT']);
function updateCardStatus(logins, pillId, discId) {
  const sourceId = PILL_SOURCE[pillId || 'wa-status'];
  // With more than one login for an account (e.g. a fresh re-connect left the
  // previous dead session behind), report the HEALTHIEST: a CONNECTED login
  // wins over a transient one, which wins over a BAD_CREDENTIALS/LOGGED_OUT one
  // — otherwise a stale leftover would mask a working new session.
  const login = logins.find(l => l.state === 'CONNECTED')
             || logins.find(l => !NEEDS_RESET.has(l.state))
             || (logins.length ? logins[0] : null);
  const state = login ? login.state : null;
  const healthy = state === 'CONNECTED';
  if (sourceId && runtime[sourceId]) {
    runtime[sourceId].connected = healthy;
    // #4: per-conversation reconnect flag reads this — a dead login (needs
    // re-pair) marks every conversation from this source as needing reconnect.
    runtime[sourceId].needsReconnect = !!(login && NEEDS_RESET.has(state));
  }
  const pill = $(pillId || 'wa-status');
  const disc = discId ? $(discId) : null;
  const phase = sourceId ? sourcePhase[sourceId] : undefined;   // #3: transient connect/import phase
  if (pill) {
    pill.classList.remove('ok', 'stale', 'busy');
    if (phase === 'connecting') {
      pill.textContent = 'Connecting…';
      pill.classList.add('busy');
    } else if (phase === 'importing') {
      pill.textContent = 'Connected — importing conversations…';
      pill.classList.add('busy');
    } else if (healthy) {
      pill.textContent = 'Connected: ' + login.name;
      pill.classList.add('ok');
    } else if (login && NEEDS_RESET.has(state)) {
      pill.textContent = 'Sync stale — reset needed (' + state + ')';
      pill.classList.add('stale');           // re-pair via the steps in this card
    } else if (login) {
      pill.textContent = 'Reconnecting… (' + state + ')';
      pill.classList.add('stale');
    } else {
      pill.textContent = 'Not connected';
    }
    if (disc) {
      if (login) { disc.classList.remove('hidden'); disc.dataset.loginId = login.id; }
      else disc.classList.add('hidden');
    }
  }
  notifyPlatformRail();
}

// ---- iMessage Connections card (Phase 2 B2 / P2.5) ----
// Renders the bot's plain-text checklist reply via sanitize + textContent.
function updateImsgCard(rawBody) {
  const ul = $('imsg-checklist');
  if (!ul) return;
  ul.replaceChildren();
  const clean = sanitize(rawBody);                 // keeps \n; strips controls/bidi
  const lines = clean.split('\n').map(l => l.trim()).filter(Boolean);
  for (const line of lines) ul.appendChild(el('li', '', sanitize(line)));
  const pill = $('imsg-status');
  if (pill) {
    // The daemon marks each permission [ok] / [--] (definitely missing) / [??]
    // (can't probe). Full Disk Access is the one that must be [ok] for the
    // bridge to read Messages; Accessibility/Automation are unprobeable ([??])
    // and only matter for SENDING, so "unknown" is NOT "not set up". Ready =
    // at least one [ok] and no definite [--] failure. (The daemon emits these
    // markers, never the ✓/✗ this used to look for — hence the pill never lit.)
    const hasStatus = /\[(ok|--|\?\?)\]/.test(clean);
    const failed = /\[--\]/.test(clean);
    const ok = /\[ok\]/.test(clean) && !failed;
    pill.textContent = !hasStatus ? 'No status yet' : (ok ? 'Ready' : 'Setup needed');
    pill.classList.toggle('ok', ok);
    runtime.imessage.connected = ok;
    notifyPlatformRail();
  }
}

// Confirmation modal; type-to-confirm for the most destructive action.
function confirmModal(title, text, typed) {
  return new Promise((resolve) => {
    $('modal-title').textContent = title;
    $('modal-text').textContent = text;
    const input = $('modal-input');
    input.value = '';
    input.classList.toggle('hidden', !typed);
    $('modal-backdrop').classList.remove('hidden');
    const done = (ok) => {
      $('modal-backdrop').classList.add('hidden');
      $('modal-ok').onclick = null; $('modal-cancel').onclick = null;
      resolve(ok);
    };
    $('modal-ok').onclick = () => {
      if (typed && input.value !== 'delete') return;
      done(true);
    };
    $('modal-cancel').onclick = () => done(false);
  });
}

// ---- one-click Google Messages connect (loopback helper on :8020) ----------
// The helper is a local-only service that reads Chrome cookies + drives the
// bridge login on the browser's behalf. We reach it with fetch() (NOT api(),
// which targets the Matrix homeserver). The custom X-Beepa-Connect header +
// application/json content-type force a CORS preflight, and the helper only
// echoes this app's origin — so only this local app can drive a connect.
const GMSG_CONNECT_BASE = 'http://127.0.0.1:8020';
const GMSG_CONNECT_HEADERS = { 'Content-Type': 'application/json', 'X-Beepa-Connect': '1' };

async function runGmessagesConnect(btn, out, fallback) {
  btn.disabled = true;
  fallback.classList.add('hidden');
  out.replaceChildren();
  // Open the Google sign-in tab first, so session cookies exist for the helper
  // to read. noopener: the new tab gets no handle back to this window.
  window.open('https://messages.google.com/web/', '_blank', 'noopener');

  // 1. Ask the helper to start the login (reads cookies, calls the bridge).
  let start;
  try {
    const r = await fetch(GMSG_CONNECT_BASE + '/connect/gmessages/start',
      { method: 'POST', headers: GMSG_CONNECT_HEADERS, body: '{}' });
    start = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = (start && start.error) ? String(start.error) : '';
      if (/google session/i.test(msg)) {
        out.appendChild(el('p', 'muted',
          'Finish signing into Google in the tab that opened, then click Sign in & connect again.'));
      } else {
        out.appendChild(el('p', 'error',
          sanitizeLine(msg) || 'Could not start Google Messages login.'));
      }
      btn.disabled = false;
      return;
    }
  } catch (e) {
    // The helper is not running / not reachable — offer the CLI fallback.
    fallback.classList.remove('hidden');
    btn.disabled = false;
    return;
  }

  // 2. Show the emoji to tap on the phone.
  out.replaceChildren();
  out.appendChild(el('p', 'muted', 'On your phone, open Google Messages and tap this emoji:'));
  const emojiEl = el('div', 'gmsg-emoji', sanitizeLine(start && start.emoji ? String(start.emoji) : ''));
  emojiEl.style.cssText = 'font-size:32px;line-height:1.2;margin:6px 0;';
  out.appendChild(emojiEl);
  out.appendChild(el('p', 'muted', 'Waiting for you to tap it…'));

  // 3. Wait for the tap (the helper blocks on the bridge, up to ~2 min).
  let wait;
  try {
    const r = await fetch(GMSG_CONNECT_BASE + '/connect/gmessages/wait',
      { method: 'POST', headers: GMSG_CONNECT_HEADERS, body: '{}' });
    wait = await r.json().catch(() => ({}));
  } catch (e) {
    out.replaceChildren();
    out.appendChild(el('p', 'error', 'Lost contact with the connect helper. Click Sign in & connect to retry.'));
    btn.disabled = false;
    return;
  }

  out.replaceChildren();
  if (wait && wait.status === 'complete') {
    const acct = wait.account ? (' as ' + sanitizeLine(String(wait.account))) : '';
    out.appendChild(el('p', '', 'Connected' + acct + '. Your chats will sync shortly.'));
    sendCmd('gmessages', 'list-logins');   // refresh the pill → CONNECTED
  } else {
    out.appendChild(el('p', 'muted',
      (wait && wait.status === 'timeout')
        ? 'Timed out waiting for the emoji tap. Click Sign in & connect to try again.'
        : 'Login did not complete. Click Sign in & connect to try again.'));
  }
  btn.disabled = false;
}

// ---- one-click Instagram / LinkedIn / X connect (loopback helper on :8021) --
// twitter/linkedin complete SERVER-SIDE in the helper — the session never
// reaches the browser. instagram has no bridge provisioning login, so the
// helper returns its cookie blob for us to submit through the SAME guarded
// management-room path the paste box uses (sendSecretToMgmt + immediate
// redact). If the helper is unreachable, we reveal the paste box as fallback.
const SESSION_CONNECT_BASE = 'http://127.0.0.1:8021';
const SESSION_CONNECT_HEADERS = { 'Content-Type': 'application/json', 'X-Beepa-Connect': '1' };

async function runSessionConnect(net, siteUrl, warnEl, pasteEl, onDone, loginCmd) {
  if (S.busy) return;
  setSourcePhase(net, 'connecting');               // #3: immediate "Connecting…" feedback
  warnEl.classList.add('hidden'); warnEl.textContent = '';
  clearRedactRetry(warnEl);
  // Open the site first so the session cookies exist for the helper to read.
  window.open(siteUrl, '_blank', 'noopener');
  let start;
  try {
    const r = await fetch(SESSION_CONNECT_BASE + '/connect/' + net + '/start',
      { method: 'POST', headers: SESSION_CONNECT_HEADERS, body: '{}' });
    start = await r.json().catch(() => ({}));
    if (!r.ok) {
      warnEl.textContent = /session not found/i.test(String(start && start.error || ''))
        ? 'Finish signing in on the tab that opened, then click Connect again.'
        : (sanitizeLine(String(start && start.error || '')) || 'Could not connect.');
      warnEl.classList.remove('hidden');
      setSourcePhase(net, null);
      return;
    }
  } catch (e) {
    // Helper not running / unreachable — put the bot in login mode FIRST (the
    // mgmt-room login command must precede the pasted cookies, or the bot
    // ignores them), then reveal the paste fallback.
    if (loginCmd) { try { await sendCmd(net, loginCmd); } catch (_) {} }
    pasteEl.classList.remove('hidden');
    warnEl.textContent = 'One-click helper not reachable — paste your session below instead.';
    warnEl.classList.remove('hidden');
    setSourcePhase(net, null);
    return;
  }

  if (start && start.status === 'complete') {
    // twitter / linkedin: done server-side; the session never touched the browser.
    if (typeof onDone === 'function') onDone();
    setSourcePhase(net, 'importing');              // #3: show "importing conversations…"
    sendCmd(net, 'list-logins');
    return;
  }

  if (start && start.status === 'input_required') {
    // The login needs one more interactive value the cookies can't supply
    // (X's XChat passcode). Ask for it in the card and submit via /input —
    // the value rides browser -> loopback -> bridge only, never a Matrix room.
    promptSessionInput(net, start, warnEl, onDone);
    return;
  }

  if (start && start.status === 'cookies' && start.cookies) {
    // instagram: submit the returned blob through the guarded mgmt path, then
    // redact it — identical handling to the paste box. The value lives only in
    // this scope and is nulled after the send.
    S.busy = true; setButtonsDisabled(true);
    let secret = String(start.cookies);
    try {
      if (loginCmd) await sendCmd(net, loginCmd);   // put the bot in login mode
      const sent = await sendSecretToMgmt(net, secret);
      secret = null;
      if (!sent || !sent.eventId) {
        warnEl.textContent = 'Sent, but the bridge did not return a message id, so it cannot be auto-deleted. ' + ESCAPE_HINT;
        warnEl.classList.remove('hidden');
      } else {
        try { await redactMgmtEvent(sent.roomId, sent.eventId); if (typeof onDone === 'function') onDone(); setSourcePhase(net, 'importing'); }
        catch (e) { showRedactFailure(warnEl, sent.roomId, sent.eventId, onDone); }
      }
    } catch (e) {
      secret = null;
      warnEl.textContent = 'Could not send the session: ' + String(e.message || e);
      warnEl.classList.remove('hidden');
      setSourcePhase(net, null);
    } finally {
      S.busy = false; setButtonsDisabled(false);
      sendCmd(net, 'list-logins');
    }
    return;
  }

  // status: failed
  warnEl.textContent = 'Could not connect (the session may be stale — sign in again and retry).';
  warnEl.classList.remove('hidden');
  sendCmd(net, 'list-logins');
}

// Render the interactive step the bridge asked for (e.g. X's XChat passcode)
// as fields inside the bridge card, and submit them to the helper's /input
// endpoint. Mounts on the card (not the hidden paste box) so it is always
// visible. Values are treated like passwords: never echoed back, cleared from
// the inputs before the network call, and dropped after. textContent/el() only.
function promptSessionInput(net, start, warnEl, onDone) {
  const mount = (warnEl.closest && warnEl.closest('.bridge-card')) || warnEl.parentNode;
  const prev = mount.querySelector('.sc-input');   // one prompt at a time
  if (prev) prev.remove();

  const box = el('div', 'sc-input');
  box.style.cssText = 'margin-top:10px;';
  if (start.instructions) box.appendChild(el('p', 'muted', sanitize(String(start.instructions))));

  const inputs = [];
  const fields = Array.isArray(start.fields) ? start.fields : [];
  for (const f of fields) {
    if (!f || !f.id) continue;
    const label = el('label', 'muted', sanitizeLine(String(f.name || f.id)));
    label.style.cssText = 'display:block;margin-top:6px;';
    const inp = el('input');
    const kind = (String(f.type || '') + ' ' + String(f.id || '')).toLowerCase();
    // Every field in a bridge login step is a credential, so mask by default;
    // only obviously-plain identifiers get a visible text box.
    const isPlain = /^(text|email|username|user|phone|tel|url|name)$/.test(String(f.type || '').toLowerCase());
    inp.type = isPlain ? 'text' : 'password';
    inp.autocomplete = 'off';
    inp.spellcheck = false;
    if (/code|pin|otp|2fa/.test(kind)) inp.inputMode = 'numeric';
    inp.style.cssText = 'width:100%;box-sizing:border-box;margin-top:4px;';
    label.appendChild(inp);
    box.appendChild(label);
    inputs.push([String(f.id), inp]);
  }

  const warn = el('p', 'error hidden');
  warn.style.cssText = 'margin:6px 0 0;';
  const submit = el('button', 'primary', 'Submit');
  submit.style.width = 'auto';
  const row = el('div', 'bridge-actions');
  row.appendChild(submit);
  box.appendChild(row);
  box.appendChild(warn);
  mount.appendChild(box);
  if (inputs.length) inputs[0][1].focus();

  submit.addEventListener('click', async () => {
    if (S.busy) return;
    warn.classList.add('hidden'); warn.textContent = '';
    const values = {};
    for (const [id, inp] of inputs) values[id] = inp.value;
    for (const [, inp] of inputs) inp.value = '';   // clear the credential immediately
    S.busy = true; setButtonsDisabled(true); submit.disabled = true;
    try {
      const r = await fetch(SESSION_CONNECT_BASE + '/connect/' + net + '/input',
        { method: 'POST', headers: SESSION_CONNECT_HEADERS,
          body: JSON.stringify({ login_id: start.login_id, step_id: start.step_id, values }) });
      const res = await r.json().catch(() => ({}));
      if (res && res.status === 'complete') {
        box.remove();
        if (typeof onDone === 'function') onDone();
      } else if (res && res.status === 'input_required') {
        box.remove();
        promptSessionInput(net, res, warnEl, onDone);   // bridge wants another step
      } else {
        warn.textContent = 'That didn’t work — check the value and try again.';
        warn.classList.remove('hidden');
      }
    } catch (e) {
      warn.textContent = 'Could not submit — the one-click helper may have stopped.';
      warn.classList.remove('hidden');
    } finally {
      S.busy = false; setButtonsDisabled(false); submit.disabled = false;
      sendCmd(net, 'list-logins');
    }
  });
}

let connectionsBuilt = false;

function ensureConnections() {
  if (!connectionsBuilt) buildConnections();
}

// ---- Connections view ----
function buildConnections() {
  const holder = $('bridge-cards');
  if (!holder) return;
  holder.replaceChildren();

  // WhatsApp card
  const wa = el('div', 'bridge-card settings-bridge');
  wa.id = 'bridge-card-whatsapp';
  const waHead = el('div', 'bridge-head');
  waHead.appendChild(el('span', 'bridge-name', WA.label));
  const waPill = el('span', 'status-pill', 'Checking…');
  waPill.id = 'wa-status';
  waHead.appendChild(waPill);
  wa.appendChild(waHead);
  wa.appendChild(el('p', 'muted', WA.blurb));

  const waActions = el('div', 'bridge-actions');
  const connect = el('button', 'primary', 'Connect (scan QR)');
  connect.style.width = 'auto';
  connect.addEventListener('click', () => sendCmd('whatsapp', 'login qr'));
  waActions.appendChild(connect);

  const cancel = el('button', '', 'Cancel login');
  cancel.id = 'btn-cancel-login';
  cancel.classList.add('hidden');
  cancel.addEventListener('click', () => sendCmd('whatsapp', 'cancel'));
  waActions.appendChild(cancel);

  const disc = el('button', 'danger', 'Disconnect');
  disc.id = 'btn-disconnect';
  disc.classList.add('hidden');
  disc.addEventListener('click', async () => {
    const id = disc.dataset.loginId;
    if (!id) return;
    if (await confirmModal('Disconnect WhatsApp?',
      'This unlinks the bridge from your WhatsApp account. Bridged rooms stay until deleted.', false)) {
      await sendCmd('whatsapp', 'logout ' + id);
      sendStatusRefresh();
    }
  });
  waActions.appendChild(disc);

  const refresh = el('button', '', 'Refresh status');
  refresh.addEventListener('click', sendStatusRefresh);
  waActions.appendChild(refresh);
  wa.appendChild(waActions);

  const qrBox = el('div', 'qr-box hidden');
  qrBox.id = 'qr-box';
  wa.appendChild(qrBox);
  holder.appendChild(wa);

  // iMessage card (B2 hub-side)
  const im = el('div', 'bridge-card settings-bridge');
  im.id = 'bridge-card-imessage';
  const imHead = el('div', 'bridge-head');
  imHead.appendChild(el('span', 'bridge-name', IMSG.label));
  const imPill = el('span', 'status-pill', 'Checking…');
  imPill.id = 'imsg-status';
  imHead.appendChild(imPill);
  im.appendChild(imHead);
  im.appendChild(el('p', 'muted', IMSG.blurb));

  const imActions = el('div', 'bridge-actions');
  const setup = el('button', 'primary', 'Set up iMessage');
  setup.style.width = 'auto';
  setup.addEventListener('click', () => sendCmd('imessage', 'setup'));
  imActions.appendChild(setup);

  const imStatus = el('button', '', 'Check status');
  imStatus.addEventListener('click', () => sendCmd('imessage', 'status'));
  imActions.appendChild(imStatus);
  im.appendChild(imActions);

  const checklist = el('ul', 'checklist');
  checklist.id = 'imsg-checklist';
  im.appendChild(checklist);
  holder.appendChild(im);

  // Google Messages card — ONE-CLICK connect via the loopback helper (:8020).
  // The browser can't read Chrome cookies or `docker exec` the bridge, so the
  // local helper (gmessages-connect/connect_server.py) does it; the only manual
  // step is tapping the emoji it returns, on the phone. All output is built with
  // el()/textContent/sanitizeLine — never innerHTML.
  const gm = el('div', 'bridge-card settings-bridge');
  gm.id = 'bridge-card-gmessages';
  const gmHead = el('div', 'bridge-head');
  gmHead.appendChild(el('span', 'bridge-name', GMSG.label));
  const gmPill = el('span', 'status-pill', 'Checking…');
  gmPill.id = 'gmsg-status';
  gmHead.appendChild(gmPill);
  gm.appendChild(gmHead);
  gm.appendChild(el('p', 'muted', GMSG.blurb));

  const gmActions = el('div', 'bridge-actions');
  const gmConnect = el('button', 'primary', 'Sign in & connect');
  gmConnect.style.width = 'auto';
  gmActions.appendChild(gmConnect);

  const gmRefresh = el('button', '', 'Refresh status');
  gmRefresh.addEventListener('click', () => sendCmd('gmessages', 'list-logins'));
  gmActions.appendChild(gmRefresh);
  gm.appendChild(gmActions);

  // Live output region (emoji prompt / result). textContent-only.
  const gmOut = el('div', 'gmsg-connect-out');
  gmOut.style.cssText = 'margin:10px 0 0;';
  gm.appendChild(gmOut);

  // Muted fallback, revealed only if the loopback helper is unreachable.
  const gmFallback = el('p', 'muted hidden', 'Or run: python3 gmessages-connect/connect.py');
  gmFallback.style.cssText = 'margin:8px 0 0;';
  gm.appendChild(gmFallback);

  gmConnect.addEventListener('click', () => runGmessagesConnect(gmConnect, gmOut, gmFallback));
  holder.appendChild(gm);

  // Instagram card (mirrors the gmessages card, but with a session PASTE flow
  // instead of a QR — SPEC §5/§6). The pasted value is a bearer credential:
  // it is sent through the C-1 mgmt guard, redacted immediately, and never
  // logged, sanitize-rendered, persisted, or turned into a URL.
  const ig = el('div', 'bridge-card settings-bridge');
  ig.id = 'bridge-card-instagram';
  const igHead = el('div', 'bridge-head');
  igHead.appendChild(el('span', 'bridge-name', IG.label));
  const igPill = el('span', 'status-pill', 'Checking…');
  igPill.id = 'ig-status';
  igHead.appendChild(igPill);
  ig.appendChild(igHead);
  ig.appendChild(el('p', 'muted', IG.blurb));

  // Paste UI (built up front, revealed by Connect). textContent-only; no
  // innerHTML. The textarea value is treated like a password: never echoed.
  const igPaste = el('div', 'ig-paste hidden');
  igPaste.style.cssText = 'margin-top:10px;';
  igPaste.appendChild(el('p', 'muted',
    'Fallback (the one-click helper is not running): sign in on Instagram, then paste a Copy-as-cURL or the cookies JSON below and Submit.'));
  const igArea = el('textarea');
  igArea.placeholder = 'Paste your Instagram session here (the connect helper puts it on your clipboard)';
  igArea.rows = 4;
  igArea.autocomplete = 'off';
  igArea.spellcheck = false;
  igArea.style.cssText = 'width:100%;box-sizing:border-box;font-family:monospace;resize:vertical;';
  igPaste.appendChild(igArea);
  const igWarn = el('p', 'error hidden');           // visible warnings (never the secret)
  igWarn.style.cssText = 'margin:6px 0 0;';
  const igSubmit = el('button', 'primary', 'Submit session');
  igSubmit.style.width = 'auto';
  const igSubmitRow = el('div', 'bridge-actions');
  igSubmitRow.appendChild(igSubmit);
  igPaste.appendChild(igSubmitRow);
  igPaste.appendChild(igWarn);

  const igActions = el('div', 'bridge-actions');
  const igConnect = el('button', 'primary', 'Connect Instagram');
  igConnect.style.width = 'auto';
  igConnect.addEventListener('click', () => runSessionConnect(
    'instagram', 'https://www.instagram.com/', igWarn, igPaste,
    () => igPaste.classList.add('hidden'), 'login instagram'));
  igActions.appendChild(igConnect);

  const igDisc = el('button', 'danger', 'Disconnect');
  igDisc.id = 'btn-ig-disconnect';
  igDisc.classList.add('hidden');
  igDisc.addEventListener('click', async () => {
    const id = igDisc.dataset.loginId;
    if (!id) return;
    if (await confirmModal('Disconnect Instagram?',
      'This unlinks the bridge from your Instagram account. Bridged rooms stay until deleted.', false)) {
      await sendCmd('instagram', 'logout ' + id);
      sendCmd('instagram', 'list-logins');
    }
  });
  igActions.appendChild(igDisc);

  const igRefresh = el('button', '', 'Refresh status');
  igRefresh.addEventListener('click', () => sendCmd('instagram', 'list-logins'));
  igActions.appendChild(igRefresh);
  ig.appendChild(igActions);

  // Submit: send the pasted secret through the C-1 guard, capture the event_id,
  // and redact it immediately. The value lives ONLY in this handler's scope; the
  // textarea is cleared before the network call and the value is dropped after.
  igSubmit.addEventListener('click', async () => {
    if (S.busy) return;
    igWarn.classList.add('hidden');
    igWarn.textContent = '';
    clearRedactRetry(igWarn);
    let secret = igArea.value;                       // read once; never logged
    igArea.value = '';                               // clear the field immediately
    if (!secret || !secret.trim()) {
      secret = null;
      igWarn.textContent = 'Paste your Instagram session before submitting.';
      igWarn.classList.remove('hidden');
      return;
    }
    S.busy = true; setButtonsDisabled(true); igSubmit.disabled = true;
    try {
      const sent = await sendSecretToMgmt('instagram', secret);
      secret = null;                                 // drop the credential from memory
      if (!sent || !sent.eventId) {
        igWarn.textContent = 'Sent, but the bridge did not return a message id, so it cannot be auto-deleted. ' + ESCAPE_HINT;
        igWarn.classList.remove('hidden');
      } else {
        try {
          await redactMgmtEvent(sent.roomId, sent.eventId);
          igPaste.classList.add('hidden');           // hide the paste UI on success
        } catch (e) {
          showRedactFailure(igWarn, sent.roomId, sent.eventId, () => igPaste.classList.add('hidden'));
        }
      }
    } catch (e) {
      secret = null;                                 // never surface the secret in errors
      igWarn.textContent = 'Could not send the session: ' + String(e.message || e);
      igWarn.classList.remove('hidden');
    } finally {
      S.busy = false; setButtonsDisabled(false); igSubmit.disabled = false;
      sendCmd('instagram', 'list-logins');           // refresh the pill
    }
  });

  ig.appendChild(igPaste);
  holder.appendChild(ig);

  // LinkedIn card (mirrors the Instagram card exactly: a session PASTE flow,
  // not a QR — the "Copy as cURL" carries the X-LI-Track / X-LI-Page-Instance
  // headers as well as the cookies). The pasted value is a bearer credential:
  // it is sent through the C-1 mgmt guard, redacted immediately, and never
  // logged, sanitize-rendered, persisted, or turned into a URL.
  const li = el('div', 'bridge-card settings-bridge');
  li.id = 'bridge-card-linkedin';
  const liHead = el('div', 'bridge-head');
  liHead.appendChild(el('span', 'bridge-name', LI.label));
  const liPill = el('span', 'status-pill', 'Checking…');
  liPill.id = 'li-status';
  liHead.appendChild(liPill);
  li.appendChild(liHead);
  li.appendChild(el('p', 'muted', LI.blurb));

  // Paste UI (built up front, revealed by Connect). textContent-only; no
  // innerHTML. The textarea value is treated like a password: never echoed.
  const liPaste = el('div', 'li-paste hidden');
  liPaste.style.cssText = 'margin-top:10px;';
  liPaste.appendChild(el('p', 'muted',
    'Fallback (the one-click helper is not running): sign in on LinkedIn, then DevTools → Network → a voyager request → Copy as cURL, and paste below.'));
  const liArea = el('textarea');
  liArea.placeholder = 'Fallback only — paste a Copy-as-cURL here if the helper cannot connect';
  liArea.rows = 4;
  liArea.autocomplete = 'off';
  liArea.spellcheck = false;
  liArea.style.cssText = 'width:100%;box-sizing:border-box;font-family:monospace;resize:vertical;';
  liPaste.appendChild(liArea);
  const liWarn = el('p', 'error hidden');           // visible warnings (never the secret)
  liWarn.style.cssText = 'margin:6px 0 0;';
  const liSubmit = el('button', 'primary', 'Submit session');
  liSubmit.style.width = 'auto';
  const liSubmitRow = el('div', 'bridge-actions');
  liSubmitRow.appendChild(liSubmit);
  liPaste.appendChild(liSubmitRow);
  liPaste.appendChild(liWarn);

  const liActions = el('div', 'bridge-actions');
  const liConnect = el('button', 'primary', 'Connect LinkedIn');
  liConnect.style.width = 'auto';
  liConnect.addEventListener('click', () => runSessionConnect(
    'linkedin', 'https://www.linkedin.com/', liWarn, liPaste,
    () => liPaste.classList.add('hidden'), 'login cookies'));
  liActions.appendChild(liConnect);

  const liDisc = el('button', 'danger', 'Disconnect');
  liDisc.id = 'btn-li-disconnect';
  liDisc.classList.add('hidden');
  liDisc.addEventListener('click', async () => {
    const id = liDisc.dataset.loginId;
    if (!id) return;
    if (await confirmModal('Disconnect LinkedIn?',
      'This unlinks the bridge from your LinkedIn account. Bridged rooms stay until deleted.', false)) {
      await sendCmd('linkedin', 'logout ' + id);
      sendCmd('linkedin', 'list-logins');
    }
  });
  liActions.appendChild(liDisc);

  const liRefresh = el('button', '', 'Refresh status');
  liRefresh.addEventListener('click', () => sendCmd('linkedin', 'list-logins'));
  liActions.appendChild(liRefresh);
  li.appendChild(liActions);

  // Submit: send the pasted secret through the C-1 guard, capture the event_id,
  // and redact it immediately. The value lives ONLY in this handler's scope; the
  // textarea is cleared before the network call and the value is dropped after.
  liSubmit.addEventListener('click', async () => {
    if (S.busy) return;
    liWarn.classList.add('hidden');
    liWarn.textContent = '';
    clearRedactRetry(liWarn);
    let secret = liArea.value;                       // read once; never logged
    liArea.value = '';                               // clear the field immediately
    if (!secret || !secret.trim()) {
      secret = null;
      liWarn.textContent = 'Paste your LinkedIn session before submitting.';
      liWarn.classList.remove('hidden');
      return;
    }
    S.busy = true; setButtonsDisabled(true); liSubmit.disabled = true;
    try {
      const sent = await sendSecretToMgmt('linkedin', secret);
      secret = null;                                 // drop the credential from memory
      if (!sent || !sent.eventId) {
        liWarn.textContent = 'Sent, but the bridge did not return a message id, so it cannot be auto-deleted. ' + ESCAPE_HINT;
        liWarn.classList.remove('hidden');
      } else {
        try {
          await redactMgmtEvent(sent.roomId, sent.eventId);
          liPaste.classList.add('hidden');           // hide the paste UI on success
        } catch (e) {
          showRedactFailure(liWarn, sent.roomId, sent.eventId, () => liPaste.classList.add('hidden'));
        }
      }
    } catch (e) {
      secret = null;                                 // never surface the secret in errors
      liWarn.textContent = 'Could not send the session: ' + String(e.message || e);
      liWarn.classList.remove('hidden');
    } finally {
      S.busy = false; setButtonsDisabled(false); liSubmit.disabled = false;
      sendCmd('linkedin', 'list-logins');            // refresh the pill
    }
  });

  li.appendChild(liPaste);
  holder.appendChild(li);

  // X (Twitter) card (mirrors the LinkedIn card exactly: a session PASTE flow,
  // not a QR. The pasted value is a bearer credential: sent through the C-1 mgmt
  // guard, redacted immediately, and never logged, sanitize-rendered, persisted,
  // or turned into a URL.)
  const tw = el('div', 'bridge-card settings-bridge');
  tw.id = 'bridge-card-twitter';
  const twHead = el('div', 'bridge-head');
  twHead.appendChild(el('span', 'bridge-name', TW.label));
  const twPill = el('span', 'status-pill', 'Checking\u2026');
  twPill.id = 'tw-status';
  twHead.appendChild(twPill);
  tw.appendChild(twHead);
  tw.appendChild(el('p', 'muted', TW.blurb));

  const twPaste = el('div', 'tw-paste hidden');
  twPaste.style.cssText = 'margin-top:10px;';
  twPaste.appendChild(el('p', 'muted',
    'Fallback (the one-click helper is not running): sign in on X, then DevTools \u2192 Network \u2192 a request \u2192 Copy as cURL, and paste below.'));
  const twArea = el('textarea');
  twArea.placeholder = 'Fallback only — paste a Copy-as-cURL here if the helper cannot connect';
  twArea.rows = 4;
  twArea.autocomplete = 'off';
  twArea.spellcheck = false;
  twArea.style.cssText = 'width:100%;box-sizing:border-box;font-family:monospace;resize:vertical;';
  twPaste.appendChild(twArea);
  const twWarn = el('p', 'error hidden');
  twWarn.style.cssText = 'margin:6px 0 0;';
  const twSubmit = el('button', 'primary', 'Submit session');
  twSubmit.style.width = 'auto';
  const twSubmitRow = el('div', 'bridge-actions');
  twSubmitRow.appendChild(twSubmit);
  twPaste.appendChild(twSubmitRow);
  twPaste.appendChild(twWarn);

  const twActions = el('div', 'bridge-actions');
  const twConnect = el('button', 'primary', 'Connect X');
  twConnect.style.width = 'auto';
  twConnect.addEventListener('click', () => runSessionConnect(
    'twitter', 'https://x.com/', twWarn, twPaste,
    () => twPaste.classList.add('hidden'), 'login cookies'));
  twActions.appendChild(twConnect);

  const twDisc = el('button', 'danger', 'Disconnect');
  twDisc.id = 'btn-tw-disconnect';
  twDisc.classList.add('hidden');
  twDisc.addEventListener('click', async () => {
    const id = twDisc.dataset.loginId;
    if (!id) return;
    if (await confirmModal('Disconnect X?',
      'This unlinks the bridge from your X account. Bridged rooms stay until deleted.', false)) {
      await sendCmd('twitter', 'logout ' + id);
      sendCmd('twitter', 'list-logins');
    }
  });
  twActions.appendChild(twDisc);

  const twRefresh = el('button', '', 'Refresh status');
  twRefresh.addEventListener('click', () => sendCmd('twitter', 'list-logins'));
  twActions.appendChild(twRefresh);
  tw.appendChild(twActions);

  twSubmit.addEventListener('click', async () => {
    if (S.busy) return;
    twWarn.classList.add('hidden');
    twWarn.textContent = '';
    clearRedactRetry(twWarn);
    let secret = twArea.value;                       // read once; never logged
    twArea.value = '';                               // clear the field immediately
    if (!secret || !secret.trim()) {
      secret = null;
      twWarn.textContent = 'Paste your X session before submitting.';
      twWarn.classList.remove('hidden');
      return;
    }
    S.busy = true; setButtonsDisabled(true); twSubmit.disabled = true;
    try {
      const sent = await sendSecretToMgmt('twitter', secret);
      secret = null;                                 // drop the credential from memory
      if (!sent || !sent.eventId) {
        twWarn.textContent = 'Sent, but the bridge did not return a message id, so it cannot be auto-deleted. ' + ESCAPE_HINT;
        twWarn.classList.remove('hidden');
      } else {
        try {
          await redactMgmtEvent(sent.roomId, sent.eventId);
          twPaste.classList.add('hidden');           // hide the paste UI on success
        } catch (e) {
          showRedactFailure(twWarn, sent.roomId, sent.eventId, () => twPaste.classList.add('hidden'));
        }
      }
    } catch (e) {
      secret = null;                                 // never surface the secret in errors
      twWarn.textContent = 'Could not send the session: ' + String(e.message || e);
      twWarn.classList.remove('hidden');
    } finally {
      S.busy = false; setButtonsDisabled(false); twSubmit.disabled = false;
      sendCmd('twitter', 'list-logins');             // refresh the pill
    }
  });

  tw.appendChild(twPaste);
  holder.appendChild(tw);

  // Planned sources (inert placeholders).
  const more = el('div', 'card src-placeholder');
  more.appendChild(el('h3', '', 'More sources'));
  more.appendChild(el('p', 'muted',
    'This hub is built to bridge every messaging account into one place. Each source below becomes a card like WhatsApp once its bridge is deployed on this stack.'));
  for (const name of PLANNED_SOURCES) {
    const row = el('div', 'cmd');
    const info = el('div', 'info');
    info.appendChild(el('div', 'name', name));
    info.appendChild(el('div', 'desc', 'Not connected — bridge not deployed yet.'));
    row.appendChild(info);
    more.appendChild(row);
  }
  holder.appendChild(more);
  connectionsBuilt = true;
}

let settingsBuilt = false;

function ensureSettings() {
  if (!settingsBuilt) buildSettings();
}

// ---- Settings view (per-source command surface) ----
function buildSettings() {
  const tabs = $('settings-source-tabs');
  if (!tabs) return;
  tabs.replaceChildren();
  for (const s of SOURCES) {
    if (s.kind === 'all' || !s.botMxid) continue;
    const b = el('button', 'settings-src-tab');
    b.type = 'button';
    b.dataset.src = s.id;
    b.textContent = s.label;
    b.addEventListener('click', () => {
      S.activeSettingsSource = s.id;
      renderSettingsTabs();
      renderCommandGroups(s.id);
    });
    tabs.appendChild(b);
  }
  renderSettingsTabs();
  renderCommandGroups(S.activeSettingsSource);
  settingsBuilt = true;
}
function renderSettingsTabs() {
  const tabs = $('settings-source-tabs');
  if (!tabs) return;
  for (const b of tabs.children) {
    b.classList.toggle('active', b.dataset.src === S.activeSettingsSource);
  }
}
function renderCommandGroups(sourceId) {
  const holder = $('command-groups');
  if (!holder) return;
  holder.replaceChildren();
  for (const g of groupsFor(sourceId)) {
    const groupEl = el('div', 'settings-cmd-group');
    groupEl.appendChild(el('h3', '', g.title));
    for (const c of g.cmds) {
      const row = el('div', 'cmd');
      const info = el('div', 'info');
      info.appendChild(el('div', 'name', c.label));
      info.appendChild(el('div', 'desc', c.desc));
      row.appendChild(info);
      let argInput = null;
      if (c.arg) {
        argInput = el('input');
        argInput.placeholder = c.arg;
        row.appendChild(argInput);
      }
      const btn = el('button', c.confirm === 'type' ? 'danger' : '', 'Run');
      btn.addEventListener('click', async () => {
        let text = c.cmd;
        if (argInput) {
          const v = argInput.value.trim();
          if (!v) { logConsole('error', c.label + ': an argument is required (' + c.arg + ').'); return; }
          text += ' ' + v;
        }
        if (c.confirm === 'type') {
          if (!(await confirmModal(c.label, 'This is irreversible on the Matrix side. Type "delete" to confirm.', true))) return;
        } else if (c.confirm === 'click') {
          if (!(await confirmModal(c.label, c.desc + ' Continue?', false))) return;
        }
        await sendCmd(sourceId, text);
        if (argInput) argInput.value = '';
      });
      row.appendChild(btn);
      groupEl.appendChild(row);
    }
    holder.appendChild(groupEl);
  }
}

export {
  logConsole, setButtonsDisabled, setLoginFlow, updateCardStatus, updateImsgCard, confirmModal,
  buildConnections, buildSettings, ensureConnections, ensureSettings,
  renderSettingsTabs, renderCommandGroups, setPlatformRailHook,
};
