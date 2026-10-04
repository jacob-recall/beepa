import { el } from './el.js';
import { S, runtime } from '../state.js';

let activeCard = null;

export function resetDiscordLogin() {
  if (activeCard) activeCard.cancel();
}

export function discordCard(getBase, confirm) {
  const card = el('div', 'bridge-card settings-bridge');
  card.id = 'bridge-card-discord';
  const head = el('div', 'bridge-head');
  head.appendChild(el('span', 'bridge-name', 'Discord'));
  const pill = el('span', 'status-pill', 'Checking…');
  pill.id = 'discord-status';
  head.appendChild(pill);
  card.appendChild(head);
  card.appendChild(el('p', 'muted', 'Connect your Discord DMs and group DMs. Server channels are not included.'));
  card.appendChild(el('p', 'muted', 'This uses your personal Discord account. Discord may restrict unofficial clients.'));
  const actions = el('div', 'bridge-actions');
  const connect = el('button', 'primary', 'Connect (scan QR)');
  const cancel = el('button', 'hidden', 'Cancel login');
  const reconnect = el('button', 'hidden', 'Reconnect');
  const logout = el('button', 'danger hidden', 'Disconnect');
  const refresh = el('button', '', 'Refresh status');
  for (const button of [connect, cancel, reconnect, logout, refresh]) actions.appendChild(button);
  card.appendChild(actions);
  const qrBox = el('div', 'qr-box hidden');
  card.appendChild(qrBox);
  const message = el('p', 'muted');
  message.setAttribute('role', 'status');
  card.appendChild(message);
  const fallback = el('details');
  fallback.appendChild(el('summary', '', 'Use an account token instead'));
  const instructions = el('a', '', 'Discord token login instructions');
  instructions.href = 'https://docs.mau.fi/bridges/go/discord/authentication.html#token-login';
  instructions.target = '_blank';
  instructions.rel = 'noopener noreferrer';
  fallback.appendChild(instructions);
  fallback.appendChild(el('p', 'muted', 'Paste only the token here. It goes directly to your local bridge, never into a chat.'));
  const label = el('label', '', 'Discord account token');
  const token = el('input');
  token.type = 'password';
  token.autocomplete = 'off';
  token.spellcheck = false;
  token.maxLength = 4096;
  label.appendChild(token);
  fallback.appendChild(label);
  const submit = el('button', 'primary', 'Connect with token');
  fallback.appendChild(submit);
  card.appendChild(fallback);
  let sessionId = null;
  let timer = null;
  let generation = 0;
  let busy = false;

  async function request(action, body = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch((await getBase()) + '/connect/discord/' + action, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Beepa-Connect': '1' },
        body: JSON.stringify(body), signal: controller.signal,
      });
      if (!response.ok) throw new Error('Discord request failed');
      return await response.json();
    } finally {
      clearTimeout(timeout);
    }
  }

  function clearLogin() {
    clearTimeout(timer);
    timer = null;
    sessionId = null;
    qrBox.replaceChildren();
    qrBox.classList.add('hidden');
    cancel.classList.add('hidden');
  }

  function buttons() {
    for (const button of [connect, reconnect, logout, refresh, submit]) button.disabled = busy || !!sessionId;
    fallback.classList.toggle('hidden', runtime.discord.connected || !!sessionId);
    connect.classList.toggle('hidden', runtime.discord.connected);
  }

  function display(result) {
    runtime.discord.connected = result.status === 'connected';
    const labels = { connected: 'Connected', disconnected: 'Disconnected', logged_out: 'Not connected',
      starting: 'Preparing QR…', qr: 'Scan QR', failed: 'Login failed', expired: 'QR expired', cancelled: 'Login cancelled' };
    pill.textContent = labels[result.status] || 'Unavailable';
    pill.classList.toggle('ok', runtime.discord.connected);
    logout.classList.toggle('hidden', !['connected', 'disconnected'].includes(result.status));
    reconnect.classList.toggle('hidden', result.status !== 'disconnected');
    if (result.status === 'qr') {
      const matrix = result.matrix;
      if (!Array.isArray(matrix) || matrix.length < 21 || matrix.length > 185 ||
          !matrix.every(row => Array.isArray(row) && row.length === matrix.length && row.every(value => typeof value === 'boolean'))) {
        throw new Error('Invalid QR');
      }
      const canvas = el('canvas');
      const scale = Math.max(3, Math.floor(300 / matrix.length));
      canvas.width = canvas.height = matrix.length * scale;
      canvas.style.maxWidth = '100%';
      canvas.setAttribute('role', 'img');
      canvas.setAttribute('aria-label', 'Discord login QR code');
      const context = canvas.getContext('2d');
      context.fillStyle = '#fff';
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.fillStyle = '#000';
      matrix.forEach((row, rowIndex) => row.forEach((dark, column) => {
        if (dark) context.fillRect(column * scale, rowIndex * scale, scale, scale);
      }));
      qrBox.replaceChildren(canvas);
      qrBox.classList.remove('hidden');
      message.textContent = 'Scan with the Discord mobile app and approve the login.';
    }
    if (['starting', 'qr'].includes(result.status) && result.session_id) {
      sessionId = result.session_id;
      cancel.classList.remove('hidden');
      const current = generation;
      timer = setTimeout(async () => {
        try {
          const update = await request('poll', { session_id: sessionId });
          if (current === generation && S.token) display(update);
        } catch (_) {
          if (current === generation) failed();
        }
      }, 1500);
    } else {
      clearLogin();
      message.textContent = result.status === 'connected'
        ? 'Connected. Recent conversations will appear as the bridge imports them.'
        : result.status === 'failed' ? 'QR login failed. Retry, or use an account token if Discord requires a CAPTCHA.'
        : result.status === 'expired' ? 'The QR code expired. Connect again to request a new one.' : '';
    }
    buttons();
  }

  function failed() {
    const pending = sessionId;
    clearLogin();
    if (pending) request('cancel', { session_id: pending }).catch(() => {});
    runtime.discord.connected = false;
    pill.textContent = 'Unavailable';
    message.textContent = 'Could not reach Discord. Check that the bridge and local connect helper are running, then refresh status.';
    buttons();
  }

  async function run(action, body = {}) {
    if (busy) return;
    busy = true;
    const current = ++generation;
    clearTimeout(timer);
    buttons();
    message.textContent = action === 'start' ? 'Preparing a secure login…' : 'Checking Discord…';
    try {
      const result = await request(action, body);
      if (current === generation && S.token) display(result);
      else if (result.session_id) request('cancel', { session_id: result.session_id }).catch(() => {});
    } catch (_) {
      if (current === generation) failed();
    } finally {
      busy = false;
      buttons();
    }
  }

  activeCard = { cancel() {
    generation++;
    token.value = '';
    const pending = sessionId;
    clearLogin();
    if (pending) request('cancel', { session_id: pending }).catch(() => {});
    runtime.discord.connected = false;
    buttons();
  } };
  connect.addEventListener('click', () => run('start'));
  refresh.addEventListener('click', () => run('status'));
  reconnect.addEventListener('click', () => run('reconnect'));
  cancel.addEventListener('click', () => run('cancel', { session_id: sessionId }));
  logout.addEventListener('click', async () => {
    if (await confirm('Disconnect Discord?', 'This logs the bridge out. Previously imported conversations remain available.', false)) await run('logout');
  });
  submit.addEventListener('click', () => {
    const value = token.value.trim();
    token.value = '';
    if (value) run('token', { token: value });
  });
  if (S.token) run('status');
  return card;
}
