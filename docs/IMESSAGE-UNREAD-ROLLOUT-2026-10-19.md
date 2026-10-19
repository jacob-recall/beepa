# Native unread state and master read controls — 0.3.3

This fixes stale blue dots, red badges and large unread counts caused by importing
Apple history into Matrix. Previously the UI displayed Matrix notification counts;
reading the originals in Apple Messages did not change those counts.

## Master UI fix in 0.3.3

Opening a conversation with multiple messages in 0.3.2 could throw before sending
its native read request: passing the timestamp helper directly to `Array.map`
passed the array index as the timestamp-correction map. The regression test now
uses the actual timestamp helper and multiple messages, including a corrected
timestamp. The callback now passes only the event.

Native confirmation received by the open conversation now updates the existing
sidebar/feed rows immediately, including state-only sync responses. The badge
remains until source confirmation; HTTP acceptance is not a successful native read.
The real master entry point was tested in Chromium with synthetic intercepted
Matrix responses: open → read request → confirmed badge clearing, explicit unread
with the viewer held open, and explicit read. No JavaScript errors or unexpected
writes occurred. The unit/conformance suite also passed.

This patch changes the master UI only. Existing 0.3.2 source daemons and uplinks
are compatible and do not need a restart for this UI fix. Older sources still
need the native-state rollout below. Serve the updated `apps/master/main.js` and
refresh the master browser. No account, state, token or service migration is needed.

## Behavior

- iMessage badges use the signed connector's own `unreadCount`, `isUnread` and
  `isMarkedUnread` evidence. A manually unread conversation has a badge of at
  least one. Other messaging providers retain their current unread behavior.
- Reading a conversation in Apple Messages updates both Beepa views on the next
  native poll and source uplink pass, including when no new message arrives.
- Opening the latest conversation in a visible master browser requests a native
  mark-read. The latest native message must be present in the displayed history.
  A hidden tab or an older context window alone cannot authorize clearing newer
  messages. Returning to a visible tab rechecks the latest displayed evidence.
- The header has **Mark unread for user** and **Mark read for user**. Mark unread
  suppresses automatic clearing until the master closes/reopens the conversation
  or explicitly chooses Mark read. Buttons appear only with native capability.
- Counts change after source observation, not optimistically after a request PUT.
  The header distinguishes pending, confirmed, expired/changed and unconfirmed.
  An offline source does not silently apply old requests when it reconnects.
- The user app requests the same native mark-read when its existing phone-read
  preference is enabled. With that preference disabled it uses a private Matrix
  read marker; it does not mutate Apple Messages. The iMessage source badge still
  reflects the last reported Apple state. For other providers, private Matrix
  markers clear the local badge without broadcasting a public receipt.
- These are the account owner's unread indicators. They never grant blue checks
  on outgoing messages. Marking a chat unread cannot retract a read receipt already
  delivered to another person. Apple controls whether native reads send receipts.
- CRM lists open the regular conversation view and use these same controls. List
  monitoring, deadlines and master-seen acknowledgements remain canonical list
  functions; no duplicate watch/reminder store is introduced.

## Implementation and boundaries

`imessage/daemon.py` publishes bot-authored `com.jkali.imessage_read_state` in
existing mapped portals. It uses the pinned 0.24.4 CLI unchanged. The source
revision hashes the exact chat, native tail IDs/order and read position. Polling
publishes changed snapshots with a bounded write/time budget, independently of
message-tail timestamp changes. Initial correction of a large account takes
several poll cycles; no messages are marked read to erase an imported backlog.

`agents/uplink/native_read.py` accepts only the configured iMessage bot's state
on current iMessage mirrors. It persists that authority across incremental syncs
and restart, preventing later Matrix notification counts from resurrecting stale
badges. The existing durable read-state outbox publishes the projection as
`com.jkali.read_state.native`; publication failure retains a retryable projection.
The master accepts it only from the mirror creator. Unknown source state retains
legacy behavior until real native evidence arrives; it is never fabricated as zero.

Master actions are a metadata-only `com.jkali.read_action` envelope in a fixed
`com.jkali.read_request` event in the existing allowlisted proposals room. The source
adds only this type to its verified room power levels. Using a separate event type
keeps automatic read traffic out of suggestion-history pagination and queue limits.
They have no body, never become suggestions, and never
enter the message dispatcher. The source checks current manager identity, pairing
generation, exact local/mirror mapping, native capability, active link, sharing,
suspension and a two-minute expiry. They are unavailable during initial proposal
sync. The local command has the fixed `com.jkali.imessage.read_request` type.

The native daemon accepts only its exact local owner in an existing mapped portal.
It checks expiry and rereads the native revision immediately before invoking
`mark-read` or `mark-unread`. For a manual unread marker, read uses the supported
`select-chat` operation: pinned 0.24.4 can short-circuit `mark-read` based on message
read flags without clearing a manually unread chat. The exact chat is selected
and its unread state is then queried; selection alone is never confirmation.
A durable `native_read_command` fence is written before
that invocation. Replayed events and interrupted/unknown outcomes are not executed
again. Only a subsequent native chat query can confirm the desired state; CLI exit
zero alone is insufficient. The pinned commands return Void, so the mutator does
not require JSON output. Publication can retry without rerunning the native action.

The native API marks a whole chat, not an atomic read-up-to-message position. The
fresh revision check prevents delayed commands from clearing an already observed
newer tail, but a new message arriving between that check and Apple's mutation is
an unavoidable remaining race in this CLI API. Do not describe it as atomic.

## Upgrade existing Macs

Follow [the peer rollout guide](PEER-ROLLOUT-2026-10-18.md). Use the exact published
0.3.3 SHA supplied with this change. Pulling files is insufficient when services
run staged releases: use the existing supported `./update.sh --apply` workflow.
Do not re-enroll Elliot (`@notelliot:master`) or David, rotate tokens, replace the
signed CLI, reset TCC, empty databases, change sharing, or replay uncertain sends.

The central gateway must serve `shared/model/native_read.js` (new allowlisted
asset) as well as the master HTML/JS. Drain active gateway requests before restarting
its verified existing LaunchAgent. The existing central control worker also needs
a restart after its current work
settles, because its shared CRM account validator recognizes the newly advertised
read-control event permission. Keep the same container, state and pairing; never
start an additional Telegram poller. No model, notification subscription, VAPID,
account or sending-state migration is required.
On each source, activate both the existing native daemon and existing uplink plus
the user UI. New tables/metadata are additive; state version stays 2, wire version 1.

### Acceptance on each source Mac

1. Record the installed/active SHA, identity and code/state roots. Preserve the
   signed CLI hash and all state. Verify one daemon and one uplink remain running.
2. Use one explicitly approved test conversation. Read it in Apple Messages.
   Inspect only its bot-authored native snapshot: unread count zero should reach
   both Beepa views without a new message or a blanket mark-all-read.
3. In the master, press Mark unread for user. Confirm the native manual unread
   marker, user badge and master badge. Leave the master viewer open: it must not
   immediately clear the marker it just requested.
4. Press Mark read for user (or reopen the latest conversation). Confirm Apple's
   native unread count zero, then both Beepa badges. Record source confirmation,
   not just HTTP acceptance. Restore the chosen conversation's original state.
5. Check a hidden tab, a newer native message beyond displayed history, revoked
   sharing, an expired command and an offline source. None should falsely show
   successful native clearing. Reconnect/restart must not repeat expired/uncertain
   actions or resurrect Matrix-only unread counts.
6. Recheck outgoing gray/blue receipt evidence separately. These controls must not
   fabricate recipient read/delivery or change any pending message-send outcome.

## Validation recorded here

Automated unit/conformance suite passed. Disposable two-homeserver scenarios
`7_read_only_manager`, `24_read_unread`, `27_native_imessage_receipts`,
`29_crm_ordered_controls` and `31_native_unread_roundtrip`
passed. Scenario 31 runs the real uplink and native daemon handlers with only the
Apple CLI mocked; it covers native zero versus imported backlog, both manager
controls, returned confirmation, replay suppression and restart persistence.
UI tests cover authority, stale counters, visible/latest-message checks, pinned
account routing and the manual-unread hold. Native tests cover Void CLI output,
replay, wrong actor/portal, expiry, changed tails and uncertain outcomes.

On Jacob's actual Mac, native read-only queries confirmed the read/unread fields
and matching list/single-chat revisions. The authorized self-chat was read (zero).
Direct CLI calls from the agent session could not obtain Accessibility, despite
the existing CLI grant. Running through the original owner-session LaunchAgent
resolved that distinction; no grants, executable or credentials were replaced.
The real master API → source uplink → native daemon → Apple → master round trip
confirmed Mark unread (count 1) and Mark read (count 0) on the authorized self-chat,
including a final repeat using the dedicated `com.jkali.read_request` transport.
This caught the manual-unread edge case above; selecting the exact chat cleared
it and native observation confirmed the result. The self-chat was restored to its
original read state.

Jacob's existing native daemon and uplink were restarted with retained SQLite
backups after confirming no active/uncertain outgoing dispatch or pending sends.
Their native projections now reach the central master. David/Elliot's acceptance
remains a source-Mac rollout gate, not a result of local tests. Central asset HTTP
checks passed after a drained gateway restart. The same existing central control
container was restarted after its command/outgoing queues settled; authenticated
manager ping returned 200. No additional worker was created.
