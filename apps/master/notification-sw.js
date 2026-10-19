// Push-only worker: activate updates immediately; there is no fetch cache to migrate.
self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
// Receipts prove the browser accepted display, not that Focus showed a banner.
async function acknowledge(token, stage) {
  if (typeof token !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(token)) return;
  const configured = new URL(self.location.href).searchParams.get('api');
  const base = new URL(configured || self.location.origin);
  // Enrollment uses the same hostname (loopback's separate port or TLS gateway).
  if (base.hostname !== new URL(self.location.origin).hostname ||
      !(base.protocol === 'https:' || (base.protocol === 'http:' && ['127.0.0.1','localhost'].includes(base.hostname)))) return;
  try {
    await fetch(new URL('/admin/notifications/receipt', base), {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({token,stage}), credentials:'omit'});
  } catch {} // Display must not fail if the master is temporarily unreachable.
}
// A push has visible user value; no credentials, conversation sends or caching.
self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let value;
    try { value = event.data.json(); } catch { return; }
    if (!value || typeof value.title !== 'string') return;
    const url = new URL(value.url || 'index.html#notifications', self.registration.scope);
    if (url.origin !== self.location.origin || !url.pathname.endsWith('/apps/master/index.html')) return;
    await self.registration.showNotification(value.title.slice(0, 180), {
      body: typeof value.body === 'string' ? value.body.slice(0, 300) : '',
      tag: String(value.tag || 'beepa'), renotify: true,
      icon: '../../shared/assets/motherload_master.png', data: { url: url.href, receipt_token: value.receipt_token },
    });
    await acknowledge(value.receipt_token, 'shown');
  })());
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const url = new URL(event.notification.data?.url || 'index.html#notifications', self.registration.scope);
    if (url.origin !== self.location.origin || !url.pathname.endsWith('/apps/master/index.html')) return;
    const receipt = acknowledge(event.notification.data?.receipt_token, 'clicked');
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const current = windows.find(client => new URL(client.url).pathname === url.pathname);
    if (current) { await current.navigate(url.href); await current.focus(); }
    else await self.clients.openWindow(url.href);
    await receipt;
  })());
});
