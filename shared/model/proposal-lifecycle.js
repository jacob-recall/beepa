// Approval and transport are separate. None of these display states grants a send.
export const PROPOSAL_DECISION = 'com.jkali.proposal_decision';
export const PROPOSAL_CLAIM = 'com.jkali.proposal_claim';
export const PROPOSAL_STATUS = 'com.jkali.proposal_status';
export function proposalDecision(events, id, owner) {
  const list = Array.isArray(events) ? events : [];
  const claims = list.filter(e => e?.type === PROPOSAL_CLAIM && e.state_key === id && e.sender === owner && e.content?.schema === 1 && e.content.proposal_event_id === id && ['accepted', 'declined'].includes(e.content.decision));
  const matches = list.filter(e => e?.type === PROPOSAL_DECISION && e.state_key === id);
  if (matches.length !== 1) return matches.length === 0 && claims.length === 1 ? claims[0].content : null;
  const e = matches[0], raw = e.content;
  const claim = claims.length === 1 ? claims[0].content : null;
  const c = claim && (raw?.claim_id !== claim.decision_event_id || raw?.decision !== claim.decision) ? claim : raw;
  return e.sender === owner && c?.schema === 1 && c.proposal_event_id === id
    && ['accepted', 'declined'].includes(c.decision)
    && ['sending', 'sent', 'unknown', 'not_sent', 'none', 'scheduled'].includes(c.send_state) ? c : null;
}
export function proposalLabel(p) {
  const s = p.status || p;
  if (s.approval?.decision === 'approve') return proposalLabel({ decision: 'accepted', send_state: s.state === 'sent' ? 'sent' : s.state === 'refused' ? 'not_sent' : s.state === 'approval_pending' ? 'sending' : 'unknown' });
  if (s.decision === 'declined' || s.state === 'declined') return 'Declined';
  if (s.decision === 'accepted') return 'Accepted · ' + ({ sent: 'sent', sending: 'send awaiting confirmation', unknown: 'send unconfirmed', not_sent: 'not sent', none: 'not sent', scheduled: 'scheduled' }[s.send_state] || 'checking send');
  return ({ sent: 'Sent directly', auto: 'Sent directly', refused: 'Not sent', ambiguous: 'Send unconfirmed',
    outcome_unknown: 'Send unconfirmed', approval_pending: 'Accepted · checking send', canceled: 'Canceled',
    accepted: 'Sending through account', waiting_for_approval: 'Queued · awaiting user', pending: 'Queued · awaiting user',
    seen: 'Queued · awaiting user', scheduled: 'Scheduled direct send' })[s.state] || 'Checking status';
}

// Read proposal-only pages so receipt/CRM status traffic cannot evict a queue.
// Refuse an incomplete history rather than silently replacing it with a tail.
export async function proposalHistory(api, room) {
  const base = '/_matrix/client/v3/rooms/' + encodeURIComponent(room) + '/messages?dir=b&limit=100&filter=' + encodeURIComponent(JSON.stringify({ types: ['com.jkali.proposal'] }));
  const events = [], cursors = new Set(); let from = '';
  for (let page = 0; page < 100; page++) {
    const data = await api('GET', base + (from ? '&from=' + encodeURIComponent(from) : ''));
    if (!Array.isArray(data?.chunk)) throw new Error('Suggestion history could not be verified');
    events.push(...data.chunk);
    if (!data.end) return events;
    if (cursors.has(data.end)) throw new Error('Suggestion history cursor repeated');
    cursors.add(data.end); from = data.end;
  }
  throw new Error('Suggestion history exceeds the supported 10,000-event window');
}
