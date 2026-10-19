// Canonical CRM storage. The Matrix timeline is authoritative; this module has
// no localStorage/IndexedDB copy of contact or message text and no native sends.
// api/bootstrap must be bound to the captured session by the app adapter.
import {
  createCRMState, applyVerifiedCRMOperation, canonicalCRMJSON,
  CRM_OPERATION_TYPE, CRM_MAX_BYTES,
} from '../../shared/model/crm.js';

const ROOM = /^![A-Za-z0-9._~=/+-]+:[A-Za-z0-9.:-]+$/;
const EVENT = /^\$[^\s]{1,255}$/;
const encoder = new TextEncoder();

export function createCRMStore({ api, bootstrap, managerMxid, isCurrent = () => true, onChange = () => {} }) {
  let roomId = null, state = null, cursor = null, ready = false, closed = false;
  let refreshing = null, appending = Promise.resolve();
  const seenEvents = new Set();
  const appliedEvents = new Set();
  function active() {
    if (closed || !isCurrent()) throw new Error('CRM session changed');
  }
  function publish() { active(); onChange({ state, ready, roomId }); }
  async function bind() {
    active();
    const info = await bootstrap();
    active();
    if (!info || info.manager_mxid !== managerMxid || info.schema_version !== 1
        || typeof info.room_id !== 'string' || !ROOM.test(info.room_id)
        || (roomId && roomId !== info.room_id)) throw new Error('CRM workspace identity changed');
    if (!roomId) {
      roomId = info.room_id;
      state = createCRMState({ workspace_id: roomId, manager_mxid: managerMxid });
    }
  }
  async function readPages(rebuilds = 0) {
    active(); ready = false;
    if (!roomId) await bind();
    let nextState = state, nextCursor = cursor;
    const nextSeen = new Set(seenEvents), visited = new Set();
    const nextApplied = new Set(appliedEvents);
    // Bound work per refresh. If more history remains, publish partial loading
    // progress but leave writes disabled until a subsequent refresh reaches end.
    for (let page = 0; page < 200; page++) {
      active();
      const filter = encodeURIComponent(JSON.stringify({ types: [CRM_OPERATION_TYPE, 'm.room.redaction'] }));
      const data = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId)
        + '/messages?dir=f&limit=100&filter=' + filter
        + (nextCursor ? '&from=' + encodeURIComponent(nextCursor) : ''));
      active();
      if (!data || !Array.isArray(data.chunk) || data.chunk.length > 100
          || (data.end !== undefined && typeof data.end !== 'string')) {
        throw new Error('Incomplete CRM history');
      }
      for (const event of data.chunk) {
        if (!event || typeof event.event_id !== 'string' || !EVENT.test(event.event_id)) {
          throw new Error('Invalid CRM history event');
        }
        if (nextSeen.has(event.event_id)) continue;
        if (event.sender !== managerMxid || Object.hasOwn(event, 'state_key')
            || (event.room_id !== undefined && event.room_id !== roomId)) {
          throw new Error('Unverified CRM operation');
        }
        if (event.type === 'm.room.redaction') {
          // Redacting a canonical operation requires a full rebuild. A partial
          // cache may otherwise keep applying an operation that no longer exists.
          const target = event.content?.redacts || event.redacts;
          if (nextApplied.has(target)) {
            if (rebuilds >= 3) throw new Error('CRM history changed during replay; refresh again');
            state = createCRMState({ workspace_id: roomId, manager_mxid: managerMxid });
            cursor = null; seenEvents.clear(); appliedEvents.clear();
            return readPages(rebuilds + 1);
          }
          nextSeen.add(event.event_id);
          continue;
        }
        if (event.type !== CRM_OPERATION_TYPE) {
          throw new Error('Unverified CRM operation');
        }
        // Redacted original events have no operation payload. Keep their event
        // ID, but never resurrect their former operation from a browser cache.
        if (event.unsigned?.redacted_because) { nextSeen.add(event.event_id); continue; }
        const result = await applyVerifiedCRMOperation(nextState, event.content);
        active();
        nextState = result.state;
        nextSeen.add(event.event_id);
        nextApplied.add(event.event_id);
      }
      const end = data.end;
      if (!end || end === nextCursor) {
        if (data.chunk.length && end === nextCursor) throw new Error('CRM history cursor did not advance');
        ready = true;
        break;
      }
      if (visited.has(end)) throw new Error('CRM history cursor loop');
      visited.add(end); nextCursor = end;
      // An empty filtered page can still advance through unrelated events.
      // Continue until the server explicitly stops, not merely chunk.length=0.
    }
    active(); state = nextState; cursor = nextCursor;
    seenEvents.clear(); for (const id of nextSeen) seenEvents.add(id);
    appliedEvents.clear(); for (const id of nextApplied) appliedEvents.add(id);
    publish();
    return { state, ready, roomId };
  }
  function refresh() {
    active();
    if (!refreshing) {
      refreshing = readPages().catch(error => {
        ready = false;
        if (!closed && isCurrent()) publish();
        throw error;
      }).finally(() => { refreshing = null; });
    }
    return refreshing;
  }
  function append(operation) {
    // Freeze caller intent before entering the queue. A changed draft cannot
    // reuse this operation/transaction ID while an earlier request is pending.
    const serialized = canonicalCRMJSON(operation);
    if (encoder.encode(serialized).length > CRM_MAX_BYTES) throw new Error('CRM operation too large');
    const frozen = JSON.parse(serialized);
    const work = async () => {
      active();
      await bind(); // Freshly verify creator/privacy/permissions before a write.
      await refresh();
      active();
      if (!ready) throw new Error('CRM history is still loading');
      const preview = await applyVerifiedCRMOperation(state, frozen);
      active();
      if (!preview.outcome.accepted || preview.outcome.duplicate) return preview.outcome;
      const result = await api('PUT', '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId)
        + '/send/com.jkali.crm.operation/' + encodeURIComponent('crm_' + frozen.op_id), frozen);
      active();
      if (!result || typeof result.event_id !== 'string' || !EVENT.test(result.event_id)) {
        throw new Error('CRM write outcome unknown; refresh before retrying');
      }
      // Server acceptance is not reducer acceptance: another tab may have won
      // the same base revision. Only canonical replay decides the result.
      await refresh();
      active();
      const observed = state.operations[frozen.op_id];
      if (!observed || observed.serialized !== serialized) {
        throw new Error('CRM operation not yet reconciled; keep its operation ID');
      }
      return { ...observed.outcome, event_id: result.event_id };
    };
    const pending = appending.then(work);
    appending = pending.catch(() => {});
    return pending;
  }
  return {
    refresh, append,
    snapshot: () => ({ state, ready, roomId }),
    close() { closed = true; ready = false; state = null; cursor = null; roomId = null; seenEvents.clear(); appliedEvents.clear(); },
  };
}
