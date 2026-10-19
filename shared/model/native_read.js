// Own source unread state. Never feed this into outgoing blue receipt checks.
export const NATIVE_READ_STATE = 'com.jkali.imessage_read_state';
export function nativeReadState(c) {
  if (!c || c.schema !== 1 || c.supported !== true || !Number.isSafeInteger(c.unread_count) || c.unread_count < 0
    || typeof c.marked_unread !== 'boolean' || !/^[a-f0-9]{64}$/.test(c.revision || '')
    || !Number.isSafeInteger(c.latest_ts) || c.latest_ts < 0
    || !Number.isSafeInteger(c.observed_ts) || c.observed_ts <= 0) return null;
  const result = { schema: 1, supported: true, unread_count: c.unread_count, marked_unread: c.marked_unread,
    revision: c.revision, latest_ts: c.latest_ts, observed_ts: c.observed_ts };
  if (/^[A-Za-z0-9_.:-]{1,128}$/.test(c.last_command?.id || '')
    && ['applied', 'changed', 'expired', 'unconfirmed', 'attempted'].includes(c.last_command?.status)) {
    result.last_command = { id: c.last_command.id, status: c.last_command.status };
  }
  return result;
}
export function nativeReadFromRoom(room, bot, previous = null) {
  let state = previous;
  for (const ev of [...(room?.state?.events || []), ...(room?.timeline?.events || [])]) {
    if (ev?.sender !== bot || ev?.type !== NATIVE_READ_STATE || ev?.state_key !== '') continue;
    const candidate = nativeReadState(ev.content);
    if (candidate && (!state || candidate.observed_ts >= state.observed_ts)) state = candidate;
  }
  return state;
}
export function canAutoRead(native, latestVisibleTs, visible, suppressed = false) {
  return !!(native && native.unread_count > 0 && visible && !suppressed
    && native.latest_ts > 0 && latestVisibleTs >= native.latest_ts);
}
