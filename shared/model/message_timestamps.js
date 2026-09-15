// Pure display metadata. Never use for send/replay authorization.
export const CORRECTION_TYPE = 'com.beepa.timestamp_correction';
export const validTimestamp = value => typeof value === 'number' &&
  Number.isSafeInteger(value) && value > 0 && value <= 8640000000000000;

export function timestampCorrections(events, sender) {
  const result = new Map();
  if (!sender) return result;
  for (const ev of events || []) {
    const c = ev && ev.content;
    if (ev && ev.type === CORRECTION_TYPE && ev.sender === sender &&
        typeof ev.state_key === 'string' && ev.state_key.startsWith('$') &&
        c && c.version === 1 && c.source === 'imessage' && validTimestamp(c.origin_ts)) {
      result.set(ev.state_key, c.origin_ts);
    }
  }
  return result;
}

export function messageTimestamp(ev, corrections) {
  const corrected = corrections && corrections.get(ev && ev.event_id);
  if (validTimestamp(corrected)) return corrected;
  const original = ev && ev.content && ev.content['com.jkali.origin_ts'];
  if (validTimestamp(original)) return original;
  return ev && typeof ev.origin_server_ts === 'number' ? ev.origin_server_ts : 0;
}
