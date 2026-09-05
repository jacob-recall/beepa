// Format only aggregate diagnostics; never display credentials or message data.
export function syncHealthText(health, now = Date.now()) {
  if (!health || typeof health !== 'object' || !Number.isFinite(health.updated_at)) {
    return 'Waiting for the background sync service to report its status.';
  }
  const age = Math.max(0, now / 1000 - health.updated_at);
  const counts = [
    ['pending_events', 'messages queued'], ['proposal_pending', 'Direct requests queued'], ['media_retry', 'attachments retrying'],
    ['history_pages_pending', 'history scans pending'], ['revocations_pending', 'sharing changes pending'],
    ['retired_revocations_pending', 'previous connection changes pending'],
  ].filter(([key]) => Number.isFinite(health[key]) && health[key] > 0)
    .map(([key, label]) => `${Math.floor(health[key])} ${label}`);
  const parts = [age > 120 ? 'Sync status is stale; the background service may be stopped.' : 'Background sync is reporting.'];
  if (health.connected === false) parts.push('The organization connection is paused or unavailable.');
  if (counts.length) parts.push(counts.join('; ') + '.');
  if (health.history_incomplete) parts.push('History catch-up is incomplete.');
  if (health.delivery_incomplete) parts.push('Delivery is incomplete; queued work will retry.');
  if (health.errors && typeof health.errors === 'object') {
    const labels = { ingestion: 'local scanning', delivery: 'delivery', history: 'history catch-up', recovery: 'master recovery', reconcile: 'sharing reconciliation', revocation: 'sharing removal', retired_revocation: 'previous connection cleanup', proposals: 'Direct requests', media: 'attachments', contacts: 'contacts' };
    const errors = Object.keys(labels).filter(key => health.errors[key]).map(key => labels[key]);
    if (errors.length) parts.push('Retrying: ' + errors.join(', ') + '.');
  }
  for (const [key, label] of [['last_ingestion', 'Last local scan'], ['last_delivery', 'Last delivery'], ['oldest_pending_ts', 'Oldest queued message']]) {
    if (Number.isFinite(health[key]) && health[key] > 0) parts.push(label + ': ' + new Date(health[key] * 1000).toLocaleString() + '.');
  }
  return parts.join(' ');
}
