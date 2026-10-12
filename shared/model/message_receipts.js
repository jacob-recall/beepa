// Receipt evidence is keyed by target event ID, never receipt/display time.
// Pure leaf shared by both apps. A group's check means at least one recipient.
export const MESSAGE_RECEIPT_TYPE = 'com.jkali.message_receipt';
export const NATIVE_RECEIPT_TYPE = 'com.jkali.imessage_receipt';
export function mergeReceipts(previous, updates) {
  const merged = new Map(previous || []);
  for (const [id, value] of updates || []) {
    if (typeof id !== 'string' || !id.startsWith('$') || id.length > 256) continue;
    const old = merged.get(id) || {};
    merged.set(id, {
      read: old.read === true || value.read === true,
      delivered: old.delivered === true || value.delivered === true || value.read === true,
      teammate_read: old.teammate_read === true || value.teammate_read === true,
      sent: old.sent === true || value.sent === true,
      failed: (old.failed === true || value.failed === true) && !old.sent && !value.sent,
    });
  }
  while (merged.size > 2000) merged.delete(merged.keys().next().value);
  return merged;
}
export function receiptPresentation(value) {
  if (value?.read) return { state: 'read', glyph: '✓✓', label: 'Read by recipient (at least one in a group)' };
  if (value?.delivered) return { state: 'delivered', glyph: '✓✓', label: 'Delivered to recipient (at least one in a group)' };
  if (value?.failed) return { state: 'failed', glyph: '!', label: 'Send failed' };
  return { state: 'sending', glyph: '✓', label: value?.sent ? 'Sent — awaiting delivery confirmation' : 'Sending — delivery not confirmed' };
}
export function paintReceipt(node, value) {
  const status = receiptPresentation(value);
  node.className = 'message-receipt ' + status.state;
  node.textContent = status.glyph;
  node.setAttribute('aria-label', status.label);
  node.setAttribute('title', status.label);
}
export function parseMessageReceipts(room, selfIds, botIds, sourceBot) {
  let out = new Map();
  for (const ev of room?.ephemeral?.events || []) {
    if (ev?.type !== 'm.receipt') continue;
    for (const [id, perEvent] of Object.entries(ev.content || {})) {
      for (const [user, info] of Object.entries(perEvent?.['m.read'] || {})) {
        if (!info || !Number.isFinite(info.ts) || info.ts < 0 || botIds.has(user)) continue;
        // Thread receipts cannot be promoted to an unthreaded conversation.
        if (info.thread_id != null && info.thread_id !== 'main') continue;
        out = mergeReceipts(out, [[id, selfIds.has(user) ? { teammate_read: true } : { read: true }]]);
      }
    }
  }
  for (const ev of [...(room?.state?.events || []), ...(room?.timeline?.events || [])]) {
    const native = ev?.type === NATIVE_RECEIPT_TYPE;
    if ((!native && ev?.type !== 'com.beeper.message_send_status') || !sourceBot || ev.sender !== sourceBot) continue;
    const c = ev.content || {}, rel = c['m.relates_to'];
    if (rel?.rel_type !== 'm.reference') continue;
    if (native && (!sourceBot.startsWith('@imessagebot:') || ev.state_key !== rel.event_id)) continue;
    const delivered = c.status === 'SUCCESS' && Array.isArray(c.delivered_to_users) &&
      c.delivered_to_users.some(user => typeof user === 'string' && user.startsWith('@') && !selfIds.has(user) && !botIds.has(user));
    const nativeDelivered = sourceBot.startsWith('@imessagebot:') && c.status === 'SUCCESS' && c['com.jkali.delivered'] === true;
    const nativeRead = native && c.status === 'SUCCESS' && c['com.jkali.read'] === true;
    out = mergeReceipts(out, [[rel.event_id, { read: nativeRead, delivered: delivered || nativeDelivered, sent: c.status === 'SUCCESS', failed: c.status === 'FAIL_PERMANENT' }]]);
  }
  return out;
}
