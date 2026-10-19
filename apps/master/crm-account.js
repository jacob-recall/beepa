// Shared read-only authority check for CRM commands and batch staging.
// The caller pins identity before awaiting and rechecks its own contact/intent
// afterwards. Display labels and cached capability flags are never authority.
export async function readCRMAccount({ api, account, ownerMxid, managerMxid, isCurrent }) {
  const check = () => { if (!isCurrent()) throw new Error('CRM session changed'); };
  check();
  const path = '/_matrix/client/v3/rooms/' + encodeURIComponent(account.proposal_room_id);
  const state = await api('GET', path + '/state'); check();
  if (!Array.isArray(state)) throw new Error('Cannot verify the sending account');
  const one = (type, key = '') => { const all = state.filter(e => e.type === type && e.state_key === key); return all.length === 1 ? all[0] : null; };
  const create = one('m.room.create'), marker = one('com.jkali.proposals'), event = one('com.jkali.crm.capability');
  const cap = event?.content, power = one('m.room.power_levels')?.content;
  const commandTypes = ['com.jkali.proposal', 'com.jkali.crm.direct_request'];
  if (cap?.batch === true || cap?.batch_cancel === true) commandTypes.push('com.jkali.crm.batch_request');
  if (cap?.conversation_tracking === true) commandTypes.push('com.jkali.monitoring_scope');
  if (cap?.native_read === true) commandTypes.push('com.jkali.read_request');
  if (create?.sender !== ownerMxid || marker?.sender !== ownerMxid
      || create.content?.type === 'm.space' || Object.hasOwn(create.content || {}, 'com.jkali.mirror_of')
      || state.some(e => ['uk.half-shot.bridge', 'm.bridge', 'm.room.encryption', 'm.room.tombstone', 'com.jkali.mirror_of'].includes(e.type))
      || event?.sender !== ownerMxid || cap?.schema !== 1 || cap.owner_mxid !== ownerMxid
      || cap.generation !== account.generation || cap.journal_epoch !== account.journal_epoch
      || cap.ordered_policy !== true || cap.direct_prepare_commit !== true
      || one('m.room.member', managerMxid)?.content?.membership !== 'join'
      || one('m.room.member', ownerMxid)?.content?.membership !== 'join'
      || state.some(e => e.type === 'm.room.member' && ![managerMxid, ownerMxid].includes(e.state_key))
      || power?.users?.[ownerMxid] !== 100 || power?.users?.[managerMxid] !== 50
      || Object.keys(power?.users || {}).length !== 2 || power?.events_default !== 100 || power?.state_default !== 100
      || power?.events?.['com.jkali.crm.direct_request'] !== 50
      || Object.entries(power?.events || {}).some(([type, level]) => !Number.isInteger(level) || (!commandTypes.includes(type) && level < 100))
      || one('m.room.join_rules')?.content?.join_rule !== 'invite'
      || !['shared', 'joined'].includes(one('m.room.history_visibility')?.content?.history_visibility)
      || one('m.room.guest_access')?.content?.guest_access !== 'forbidden') throw new Error('Sending account is not ready for CRM permissions');
  return { account, path, state, capability: cap };
}
