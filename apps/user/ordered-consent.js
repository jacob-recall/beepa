// Ordered manual consent, installed by the user app's existing consent UI.
// No conversation messages: only typed intents in the self-owned control room.
const ROOM = /^![^\s:]+:[^\s]+$/;
const ID = /^[A-Za-z0-9_.:-]{1,128}$/;
const validId = value => typeof value === 'string' && ID.test(value);
const ABSENT = Symbol('absent account data');
const revision = n => Number.isSafeInteger(n) && n >= 0;
const fail = message => { throw new Error(message); };
const encoded = encodeURIComponent;

export function createOrderedConsentWriter({ api, session, uuid = () => crypto.randomUUID(),
  wait = ms => new Promise(resolve => setTimeout(resolve, ms)), polls = 40 }) {
  let identity = null;
  let orderedSeen = false;
  const pending = new Map();
  return async function write(roomId, level, legacyWrite) {
    if (typeof roomId !== 'string' || !ROOM.test(roomId)) fail('Invalid conversation');
    if (![null, 'private', 'share', 'direct'].includes(level)) fail('Invalid sharing level');
    const captured = session();
    if (!captured.token || !captured.userId) fail('Sign in to change sharing');
    if (!identity || captured.token !== identity.token || captured.userId !== identity.userId) {
      pending.clear(); identity = captured; orderedSeen = false;
    }
    const check = () => {
      const now = session();
      if (now.token !== captured.token || now.userId !== captured.userId) fail('Sharing session changed');
    };
    const request = async (method, path, body) => { check(); const value = await api(method, path, body); check(); return value; };
    const optional = async path => {
      try { return await request('GET', path); }
      catch (error) { check(); if (error.status === 404) return ABSENT; throw error; }
    };
    const userPath = '/_matrix/client/v3/user/' + encoded(captured.userId);
    const overridePath = userPath + '/rooms/' + encoded(roomId) + '/account_data/com.jkali.share_override';
    const cap = await optional(userPath + '/account_data/com.jkali.crm_control');
    if (cap === ABSENT) {
      // Only a positive absence authorizes the pre-protocol path. Timeouts,
      // malformed capabilities and an uncertain ordered command never do.
      if (orderedSeen || pending.size) fail('Ordered sharing service is unavailable; refresh its status');
      const existing = await optional(overridePath);
      if (existing !== ABSENT && (!existing || typeof existing !== 'object' || Array.isArray(existing)
          || Object.hasOwn(existing, 'com.jkali.policy_journal'))) fail('Ordered sharing service is unavailable; refresh its status');
      check(); const result = await legacyWrite(); check(); return result;
    }
    orderedSeen = true;
    if (!cap || cap.schema !== 1 || cap.manual !== true || !ROOM.test(cap.policy_room_id)
        || !validId(cap.journal_epoch) || typeof cap.generation !== 'string' || !cap.generation
        || cap.generation.length > 128) fail('Sharing service must be updated before changing this setting');
    const controlPath = '/_matrix/client/v3/rooms/' + encoded(cap.policy_room_id);
    const state = await request('GET', controlPath + '/state');
    if (!Array.isArray(state)) fail('Cannot verify sharing controls');
    const one = (type, key = '') => {
      const matches = state.filter(e => e.type === type && e.state_key === key);
      return matches.length === 1 ? matches[0] : null;
    };
    const create = one('m.room.create'), marker = one('com.jkali.policy_control');
    if (create?.sender !== captured.userId || create.content?.type === 'm.space'
        || Object.hasOwn(create?.content || {}, 'com.jkali.mirror_of')
        || marker?.sender !== captured.userId || marker.content?.schema !== 1
        || marker.content.journal_epoch !== cap.journal_epoch
        || state.some(e => ['uk.half-shot.bridge', 'm.bridge', 'com.jkali.imessage.management', 'com.jkali.mirror_of', 'm.room.tombstone', 'm.room.encryption'].includes(e.type))
        || !state.some(e => e.type === 'm.room.member' && e.state_key === captured.userId && e.content?.membership === 'join')
        || state.some(e => e.type === 'm.room.member' && e.state_key !== captured.userId)) fail('Cannot verify sharing controls');
    const power = one('m.room.power_levels');
    if (power?.sender !== captured.userId || power.content?.users?.[captured.userId] !== 100
        || Object.keys(power.content.users).length !== 1
        || power.content.events_default !== 100 || power.content.state_default !== 100
        || ['invite', 'kick', 'ban', 'redact'].some(k => power.content[k] !== 100)
        || Object.values(power.content.events || {}).some(value => !Number.isInteger(value) || value < 100)
        || one('m.room.join_rules')?.content?.join_rule !== 'invite'
        || one('m.room.history_visibility')?.content?.history_visibility !== 'joined'
        || one('m.room.guest_access')?.content?.guest_access !== 'forbidden') fail('Cannot verify sharing controls');
    const key = roomId + ':' + (level || 'inherit');
    let intent = pending.get(key);
    if (intent && (intent.journal_epoch !== cap.journal_epoch || intent.controlRoom !== cap.policy_room_id || intent.generation !== cap.generation)) {
      fail('Sharing service changed while this operation was pending. Check the current setting before retrying.');
    }
    if (!intent) {
      const override = await optional(overridePath);
      const saved = override?.['com.jkali.policy_journal'];
      const published = one('com.jkali.policy.revision', roomId);
      let base = 0;
      if (saved) {
        if (!revision(saved.revision)) fail('Cannot verify the current sharing revision');
        base = saved.revision;
      }
      if (published) {
        const c = published.content;
        if (published.sender !== captured.userId || c?.schema !== 1 || c.room_id !== roomId
            || c.journal_epoch !== cap.journal_epoch || !revision(c.revision)) fail('Cannot verify the current sharing revision');
        base = Math.max(base, c.revision);
      }
      intent = { schema: 1, command_id: uuid(), journal_epoch: cap.journal_epoch,
        room_id: roomId, level: level || 'inherit', expected_revision: base,
        controlRoom: cap.policy_room_id, generation: cap.generation };
      pending.set(key, intent);
    }
    const { controlRoom, generation, ...payload } = intent;
    await request('PUT', controlPath + '/send/com.jkali.policy.request/' + encoded('policy_' + intent.command_id), payload);
    for (let attempt = 0; attempt < polls; attempt++) {
      const result = await optional(controlPath + '/state/com.jkali.policy.result/' + encoded(intent.command_id));
      if (result !== ABSENT) {
        if (!result || result.schema !== 1 || result.command_id !== intent.command_id || result.room_id !== roomId) fail('Sharing result did not match this change');
        if (['rejected', 'canceled'].includes(result.state)) {
          if (pending.get(key) === intent) pending.delete(key);
          fail('Sharing change refused: ' + (result.reason || result.state) + '. Refresh the current setting before trying again.');
        }
        if (result.state === 'applied') {
          const actual = await optional(overridePath), stamp = actual?.['com.jkali.policy_journal'];
          if (pending.get(key) === intent) pending.delete(key);
          if (!validId(result.operation_id) || !revision(result.revision) || stamp?.operation_id !== result.operation_id
              || stamp.revision !== result.revision || (actual?.state || null) !== level) fail('Sharing changed again; refresh to see the current setting');
          return level;
        }
      }
      if (attempt + 1 < polls) await wait(500);
      check();
    }
    fail('Sharing change is still awaiting confirmation. Retry to check the same change; the setting has not been confirmed.');
  };
}
