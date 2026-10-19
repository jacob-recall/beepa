import { PROPOSAL_DECISION, PROPOSAL_CLAIM, proposalDecision } from '../../shared/model/proposal-lifecycle.js';
// A durable decision is written before dispatch. An interrupted attempt remains
// accepted/unconfirmed and cannot return to the suggestion queue after reload.
export function createProposalDecisions({ api, session, wait = ms => new Promise(resolve => setTimeout(resolve, ms)), polls = 40 }) {
  const busy = new Set();
  return async function decide(p, decision, send) {
    const identity = session(), key = p.proposalRoomId + '/' + p.eventId;
    const current = () => { const s = session(); if (s.token !== identity.token || s.userId !== identity.userId) throw new Error('Proposal session changed'); };
    if (busy.has(key)) throw new Error('This message is already being handled');
    if (!identity.token || !identity.userId || !/^![^\s]+:[^\s]+$/.test(p.proposalRoomId || '') || !/^\$[^\s]+$/.test(p.eventId || '')) throw new Error('Unverified suggestion');
    if (!['accepted', 'declined'].includes(decision) || p.direct || p.autoSent || p.ambiguous || p.crmTracked) throw new Error('This message is not an actionable suggestion');
    busy.add(key);
    const path = '/_matrix/client/v3/rooms/' + encodeURIComponent(p.proposalRoomId) + '/state/' + PROPOSAL_DECISION + '/' + encodeURIComponent(p.eventId);
    try {
      const events = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(p.proposalRoomId) + '/state'); current();
      const existing = proposalDecision(events, p.eventId, identity.userId);
      if (existing) { p.decision = existing; throw new Error('This suggestion already has a decision. Refresh its history.'); }
      const original = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(p.proposalRoomId) + '/event/' + encodeURIComponent(p.eventId)); current();
      if (original?.sender !== identity.userId || original.type !== 'com.jkali.proposal' || original.event_id !== p.eventId || original.room_id !== p.proposalRoomId || Object.hasOwn(original, 'state_key') || original.content?.body !== p.body
        || (p.kind === 'room' ? original.content?.target_room !== p.targetRoom : original.content?.target_identifier !== p.targetIdentifier || original.content?.target_source !== p.targetSource)
        || original.content?.['com.jkali.send_mode'] === 'direct' || original.content?.['com.jkali.auto_sent'] || original.content?.['com.jkali.send_ambiguous']) throw new Error('Suggestion changed or is not actionable');
      const result = { schema: 1, proposal_event_id: p.eventId, decision_id: crypto.randomUUID(), decision, send_state: decision === 'accepted' ? 'sending' : 'none' };
      p.decision = { ...result };
      const written = await api('PUT', path, result); current();
      if (!written?.event_id) throw new Error('Decision confirmation is unavailable. Refresh history before continuing.');
      let owned = false;
      for (let n = 0; n < polls; n++) {
        const state = await api('GET', '/_matrix/client/v3/rooms/' + encodeURIComponent(p.proposalRoomId) + '/state'); current();
        const claim = (Array.isArray(state) ? state : []).find(e => e.type === PROPOSAL_CLAIM && e.state_key === p.eventId && e.sender === identity.userId)?.content;
        if (claim?.schema === 1 && claim.proposal_event_id === p.eventId) {
          if (claim.decision_event_id !== written.event_id || claim.decision !== decision) {
            p.decision = claim;
            throw new Error('Another session already handled this suggestion. Refresh its history.');
          }
          owned = true; break;
        }
        if (n + 1 < polls) await wait(500);
        current();
      }
      if (!owned) throw new Error('Decision recorded; the account sync service has not confirmed ownership. No message was sent.');
      result.claim_id = written.event_id;
      if (decision === 'accepted') {
        // No retry after an uncertain send. The original guarded app send is
        // supplied by the caller; this module never writes into a conversation.
        let ok = false;
        try { ok = await send(); } catch { /* retain unknown outcome */ }
        current(); result.send_state = ok === 'scheduled' ? 'scheduled' : ok === true ? 'sent' : 'unknown'; p.decision = { ...result };
        await api('PUT', path, result); current();
      }
      return result;
    } finally { busy.delete(key); }
  };
}
