// apps/user/orglink.js — Settings > "Connect to organization".
//
// The user pastes the master enroll URL + a one-time enrollment code (from their
// manager, generated in the master console's "Add teammate"). Connect:
//   1. redeems the code at the master's loopback/TLS enroll endpoint (CORS-scoped
//      to this origin), receiving the teammate-scoped MASTER_* credentials;
//   2. writes them to this hub's own account-data com.jkali.master_link.
// The local uplink daemon reads com.jkali.master_link each loop (agents/uplink/
// uplink.py refresh_master_config) and starts mirroring — so connecting is a
// browser action; no file editing.
//
// The daemon (the background sync service) must be installed once on this machine
// (agents/uplink/link.sh, or the launchd job). The browser cannot start it — the
// fallback command is shown for that one-time step and for remote masters whose
// origin this app's CSP does not allow.
//
// textContent only, no innerHTML. The master token lives in this hub's own
// account-data (readable only with this user's own token) — the same trust
// boundary as the rest of the app's local config.
import { api } from '../../shared/matrix/client.js';
import { $, el, sanitizeLine } from '../../shared/ui/el.js';
import { S } from '../../shared/state.js';

const LINK_TYPE = 'com.jkali.master_link';
const DEFAULT_ENROLL = 'http://127.0.0.1:8019';

// The exchange to a REMOTE master can't be a direct browser fetch — apps/user's
// CSP connect-src is loopback-only by design. So we hand {master_url, code} to
// the local session-connect helper (already in the CSP's 8021-8025 range) and it
// performs the master's /enroll/exchange server-side. Helper discovery + F1
// headers mirror the proven copy in apps/user/enrich.js / shared/ui/connections.js.
const SESSION_CONNECT_HEADERS = { 'Content-Type': 'application/json', 'X-Beepa-Connect': '1' };
let _sessionConnectBase = null;
async function sessionConnectBase() {
  if (_sessionConnectBase) return _sessionConnectBase;
  try {
    const r = await fetch('connect.local.json', { cache: 'no-store' });
    if (r.ok) {
      const j = await r.json();
      if (j && typeof j.base === 'string' && /^http:\/\/127\.0\.0\.1:\d+$/.test(j.base)) {
        _sessionConnectBase = j.base; return _sessionConnectBase;
      }
    }
  } catch (e) { /* fall back to the default below */ }
  _sessionConnectBase = 'http://127.0.0.1:8021';
  return _sessionConnectBase;
}

function adPath() {
  return '/_matrix/client/v3/user/' + encodeURIComponent(S.userId) + '/account_data/' + LINK_TYPE;
}

async function readLink() {
  try { return await api('GET', adPath()); }
  catch (e) { return {}; }   // 404 = not connected
}

export async function initOrgLinkUI() {
  const settings = $('settings-section-advanced');
  if (!settings || $('orglink-card')) return;

  const card = el('div', 'card');
  card.id = 'orglink-card';
  card.style.cssText = 'margin-bottom:var(--space-4,16px);';
  const h = el('h3', '', 'Connect to organization');
  const sub = el('p', 'muted', 'Link this account to a manager’s Beepa master so your shared conversations sync up. Ask your manager to "Add teammate" in their console; paste what they send you here.');
  card.appendChild(h);
  card.appendChild(sub);

  const status = el('p', '');
  status.id = 'orglink-status';
  card.appendChild(status);

  const urlField = el('div', 'field');
  urlField.appendChild(el('label', '', 'Master URL'));
  const urlIn = el('input', 'input');
  urlIn.id = 'orglink-url'; urlIn.value = DEFAULT_ENROLL; urlIn.spellcheck = false; urlIn.autocomplete = 'off';
  urlField.appendChild(urlIn);

  const codeField = el('div', 'field');
  codeField.style.cssText = 'margin-top:var(--space-2,9px);';
  codeField.appendChild(el('label', '', 'Enrollment code'));
  const codeIn = el('input', 'input');
  codeIn.id = 'orglink-code'; codeIn.placeholder = 'paste the one-time code'; codeIn.spellcheck = false; codeIn.autocomplete = 'off';
  codeField.appendChild(codeIn);
  card.appendChild(urlField);
  card.appendChild(codeField);

  const actions = el('div', 'bridge-actions');
  actions.style.cssText = 'margin-top:var(--space-3,13px);';
  const connectBtn = el('button', 'primary', 'Connect');
  connectBtn.style.width = 'auto';
  const disconnectBtn = el('button', 'danger', 'Disconnect');
  disconnectBtn.style.width = 'auto';
  disconnectBtn.classList.add('hidden');
  actions.appendChild(connectBtn);
  actions.appendChild(disconnectBtn);
  card.appendChild(actions);

  const warn = el('p', 'error');
  warn.id = 'orglink-warn'; warn.classList.add('hidden');
  warn.style.cssText = 'margin-top:var(--space-2,9px);';
  card.appendChild(warn);

  const note = el('p', 'muted');
  note.style.cssText = 'margin-top:var(--space-3,13px);font-size:12px;';
  note.appendChild(el('span', '', 'Any master (local or remote) works from here. If the connect helper isn’t running on this machine, enroll from a terminal instead: '));
  const cmd = el('code', '');
  cmd.style.cssText = 'display:block;margin-top:6px;padding:6px 10px;background:var(--color-neutral-100,#f9f4ed);border-radius:8px;';
  cmd.textContent = "bash agents/uplink/link.sh '<master-url>' '<code>'";
  note.appendChild(cmd);
  card.appendChild(note);

  const mount = $('command-groups') || settings;
  mount.insertBefore(card, mount.firstChild);

  function showWarn(msg) { warn.textContent = msg; warn.classList.remove('hidden'); }
  function clearWarn() { warn.textContent = ''; warn.classList.add('hidden'); }

  async function refresh() {
    const link = await readLink();
    const connected = link && link.master_token && link.master_hs_url;
    status.replaceChildren();
    if (connected) {
      status.appendChild(el('span', 'tag tag-accent-2',
        'Connected — ' + sanitizeLine(link.master_user || '') + ' @ ' + sanitizeLine(link.master_hs_url || '')));
      disconnectBtn.classList.remove('hidden');
      connectBtn.textContent = 'Reconnect';
    } else {
      status.appendChild(el('span', 'tag tag-neutral', 'Not connected'));
      disconnectBtn.classList.add('hidden');
      connectBtn.textContent = 'Connect';
    }
  }

  connectBtn.addEventListener('click', async () => {
    clearWarn();
    const base = (urlIn.value || '').trim().replace(/\/+$/, '');
    const code = (codeIn.value || '').trim();
    if (!base || !code) { showWarn('Enter the master URL and the enrollment code.'); return; }
    connectBtn.disabled = true; connectBtn.textContent = 'Connecting…';
    let creds = null;
    try {
      // Via the loopback helper (not a direct cross-origin fetch): it does the
      // master exchange server-side and returns only the credential fields.
      const helper = await sessionConnectBase();
      const res = await fetch(helper + '/enroll/exchange', {
        method: 'POST', headers: SESSION_CONNECT_HEADERS,
        body: JSON.stringify({ master_url: base, code }),
      });
      if (!res.ok) {
        showWarn('Code rejected or master unreachable (' + res.status + '). It may be used, expired, or wrong — or the connect helper isn’t running (see the command below).');
        return;
      }
      creds = await res.json();
    } catch (e) {
      showWarn('Could not reach the local connect helper. Make sure setup.sh has run on this machine, or use the command below. ' + String(e.message || e));
      return;
    } finally {
      connectBtn.disabled = false;
    }
    if (!creds || !creds.master_token || !creds.master_hs_url) {
      showWarn('The master did not return valid credentials.');
      return;
    }
    try {
      await api('PUT', adPath(), {
        master_hs_url: creds.master_hs_url, master_user: creds.master_user,
        master_token: creds.master_token, manager_mxid: creds.manager_mxid,
        master_space: creds.master_space,
      });
      codeIn.value = '';
      await refresh();
    } catch (e) {
      showWarn('Redeemed, but could not save the link locally: ' + String(e.message || e));
    }
  });

  disconnectBtn.addEventListener('click', async () => {
    clearWarn();
    try { await api('PUT', adPath(), {}); await refresh(); }
    catch (e) { showWarn('Could not disconnect: ' + String(e.message || e)); }
  });

  await refresh();
}
