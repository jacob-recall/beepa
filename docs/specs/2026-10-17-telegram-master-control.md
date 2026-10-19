# Telegram master control: implementation and operations

Implemented and verified locally, October 18, 2026. The full original requirement
audit is recorded below, including the chosen Telegram transport, canonical lists,
message-level obligations, reviewed sending, settings, context, API/UI integration
and recovery. The running controller is privately paired and ready; the browser
notification subscription remains connected. Changes are local and uncommitted.

Final model qualification caught and corrected two reminder issues: an internal
history lookup could end the command before preparing its action, and model
arithmetic could produce the wrong Unix timestamp. Context reads now explicitly
continue into the requested action; deadline tools accept ISO dates and convert
them on the server, with readable dates in the confirmation review. All six
configured-model scenarios passed after these fixes. Reasonable latency is the
product target; the configured model and timeouts were retained.

The browser delivery adapter now exposes durable enqueue/outcome APIs and records
uncertain single-message delivery without retries. Channel preference/fallback
decisions belong to the control worker's
single durable event queue, with browser push acting as an adapter. Two independent
workers must not make competing fallback decisions. Canonical lists remain the
policy authority; event/attempt state is derived operational evidence. Before
switching browser source discovery to that dispatcher, preserve existing IDs,
skip backlog, and recheck due obligations, list revisions and source access. A
provider-accepted or uncertain primary attempt must never trigger fallback.

The routing coordinator and outbox adapters are implemented in
`master/notification-routing.mjs` and `master/notification-routing-adapters.mjs`,
and wired into the service behind an explicit discovery handoff. The coordinator stores only exact source
IDs, canonical list/revision references, derived routing decisions, and delivery
outcomes. It accepts either all selected destinations or an explicit primary with
optional fallback. Fallback starts only after a definitive failed, expired-device,
or unavailable primary outcome. Pending, in-flight, unknown, canceled, and accepted
outcomes never enable it. Each list decides independently; overlapping lists share
one attempt per source event and destination. Once fallback is selected, it does
not switch back when the primary becomes available. A source event observed again
cannot acquire newly added lists or replay prior attempts.

Claims precede adapter calls. Lost adapter acknowledgments and interrupted claims
stay unknown until the adapter supplies exact-ID evidence; missing records never
prove failure. Accepted evidence is sticky. Round-robin scanning prevents old
uncertain events from blocking newer alerts. The caller must validate current list
revisions, source access, unresolved deadlines and repeat slots before every claim;
closed delivery windows defer a valid policy rather than cancel it. The existing
channel outboxes independently recheck access before delivery and retain their
grouping, provider retry and display-receipt behavior. Telegram adapters reuse old
worker keys and include retired-bot outcomes so discovery handoff cannot replay an
already attempted alert. Browser adapters require an acknowledgment for the exact
notification ID; mismatched responses are uncertain.

Canonical `list.monitoring` and `list.watch` accept `routing: null` (every selected
channel), or `{primary: "browser" | "telegram", fallback: boolean}`. Primary must
be selected in `channels`; fallback requires both channels. Watches can override
channels and routing; `inherit_monitoring` removes both overrides. The same fields
are available in reviewed Telegram/model tools and the list/conversation settings
UI. Preference controls remain disabled until authenticated status reports
`routing_ready`. The existing channel selection remains the default.

Startup calls `control_dispatcher`, which atomically freezes legacy browser
discovery and durably returns its cursors. Lost handoff replies retry the same
handoff; they never start another producer. The controller drains those positions
alongside its own cursors, reusing channel IDs and prior delivery evidence, then
deletes only the consumed import cursors. New watches start at the current stream
position. Browser reminder reconciliation remains active after handoff, including
before direct delivery calls, so resolved/snoozed obligations cannot be sent from
an old browser queue. The isolated real Synapse test covers canonical routing,
handoff, a confirmed Telegram rejection, actual browser enqueue via authenticated
HTTP, and duplicate suppression. No native messaging or push vendor is used in
that test. Deploy the updated enrollment API before the updated controller; keep
both local state databases. The helper image must also match `notifications.py`.

This is the Telegram implementation of the
[master-control design](2026-10-17-whatsapp-master-control.md). Telegram replaces
the proposed WhatsApp command channel; a new phone number is unnecessary.

## Operator workflow

### Account sync ping

Send `/ping Elliot`, `/ping David`, or `/ping all` in the paired private bot chat.
Bare `/ping` checks all permitted accounts. Exact account IDs remain accepted for
API automation; the operator sees display names. Ambiguous names ask for a more
specific account. This deterministic command works with model access disabled,
sends no native message, and changes no list or notification policy.

Manager-authenticated `POST /admin/control` with
`{"action":"ping","account":"Elliot"}` returns structured account health.
Omit `account` for all. The hosted assistant can use the same read-only
`account_health` tool for natural-language questions. The HTTP check also works
while Telegram is unavailable. Canonical `allowed_accounts` applies to both.

Each account reports two independent paths:

- **Master link:** source-owned heartbeat, last successful local ingestion,
  pending updates/receipts, and historical import gaps. A heartbeat or ingestion
  older than seven minutes is stale/stalled. A queue is “catching up”; its oldest
  native timestamp is not treated as elapsed delivery lag because it may be
  backfill. Old last delivery is normal when there has been no new shared activity.
- **iMessage importer:** native daemon heartbeat, successful/failed poll and
  pending import count. Native reports older than eight minutes are stale (the
  native five-minute heartbeat also traverses the uplink). Missing native health
  is “not verified,” including source Macs that have not installed this update.

Reports normally refresh within five minutes plus propagation time. The reply
shows its check time and each report's age: this is a recent source snapshot,
not a request/ack round trip, a native canary or proof that every Apple message
has arrived. Historical failed imports are shown separately from current liveness.

The source uplink discovers the iMessage management room through its existing
local `/sync`, then verifies full room state: bot-created room, bot-authored
management marker and health, and current local-user membership. It reads at
most once per minute and relays only explicit counters, timestamps and `poll_ok`
under `com.jkali.uplink_health.imessage`; never error text, message content, chat
identities or credentials. Failed verification/read yields unknown, never a
reused healthy result after the bounded cache expires. No native binary or TCC
permission change is required. Existing sharing/send gates are untouched.

The 0.3.1 importer adds `inbound_retryable`, `inbound_source_unavailable`,
`inbound_attachment_unavailable`, `inbound_deferred` and `poll_failed_chats`.
The original `inbound_pending` remains the unresolved total for compatibility.
Only a consistent retryable/source-unavailable breakdown is displayed as such;
older totals are unresolved work with an unknown reason, not evidence of progress.
Terminal attachment refusals are reported separately and never called a retry
queue. A successful outer poll with failed conversation scans is not healthy
ingestion. See the [importer patch handoff](../IMESSAGE-IMPORTER-ROLLOUT-2026-10-18.md)
for bounded recovery, retained state and source-local diagnostics.

Deployment: update the master worker and restart it with its existing private
state mount. Update `agents/uplink/uplink.py` and `durable_sync.py` on each source
Mac and restart that account's existing uplink service; preserve databases,
credentials and the signed iMessage binary. Wait for the next native heartbeat
(up to five minutes), then `/ping ACCOUNT` should show the importer separately.
Until a remote Mac is updated its master-link diagnosis still works.

Regression coverage: `telegram_ping.test.js` (idle/stale/failed/malformed/native
signals, names, scopes, deterministic read-only command),
`uplink_durable_sync.test.py` (verified source health and narrow publication),
`telegram_service.test.js` (authenticated/offline API), and the disposable
Synapse Telegram control scenario (source-owned state updates and API scoping).

Open the master console → Settings → Telegram. Connect through the short-lived
private link and press Start. Only the paired Telegram user in that private chat
can read data or operate Beepa. The bot is `@beepa_central_bot` on this installation.

When the worker starts processing your text message, it adds a 👀 reaction to
that exact message. This acknowledges receipt and work starting; it does not
mean the command succeeded or approve a send. Only the paired operator’s private
text messages receive this reaction, after the durable inbox claim. Duplicate
updates do not react or execute again. Buttons keep their existing callback
acknowledgement. Reaction errors are nonfatal, with a three-second request timeout,
so an unavailable reaction does not prevent the command from running.

Regression coverage: `tests/unit/telegram_reactions.test.js` checks ordering,
exact-message targeting, deduplication, authorization, callback exclusion,
reaction failures and a pairing change during acknowledgement.

Combined requests such as “send this and notify me when they reply” prepare
separate reviews for each action, even when the model produces those actions in
separate rounds. Each review has its own Confirm/Cancel buttons; confirming the
message does not confirm notifications. No action runs during preparation. Up to
four reviews can be prepared in one request. Repeated identical preparation calls
reuse their draft, and repeated confirmation never resubmits an executed send.

The assistant must finish by accounting for unprepared actions with
`finish_request`. Tool errors, provider failures and preparation limits produce
explicit incomplete-step notices. If the model skips the checklist, the runtime
requests it once and otherwise reports that only the shown reviews are ready.

`prepare_notifications` prepares incoming alerts on the exact account/conversation
through canonical lists. It reuses the enabled Followed conversations list or
prepares its creation, policy and watch in one review. If that shared list is
archived or disabled, it uses a conversation-specific reply list so unrelated
watches stay disabled. The review names the list, route and delivery policy.
Defaults are immediate browser and Telegram alerts at any time; no new deadlines
or repeats. Existing reminder deadlines and list preview settings are preserved. The reviewed
watch channels, routing and delivery hours also apply to reminders on that watch.
Global alerts must already be enabled; disabling them invalidates pending setup.

Confirmation revalidates source sharing, account access and all planned revisions.
The operations retain their individual durable IDs. A mid-plan failure leaves the
draft unknown: some metadata may have been saved, but the bot does not claim setup
succeeded or replay the plan. Verify canonical state before preparing a new review.
Restart preserves pending reviews and quarantines executing drafts as unknown.
Both reviews remain attached to an API command's durable status.

Regression: `telegram_combined_requests.test.js` covers sequential/parallel actions,
independent confirmations, duplicate preparation, partial failures, current access,
stale revisions, disabled lists and restart/API tracking. The opt-in hosted-model
qualification includes a combined send/notification request using synthetic data.

Questions about messages are read-only and finish with an answer, not an action
review. Conversation lookup accepts display names for owners and friendly app
names; conservative spelling matches stay within that scope and are labeled as
approximate candidates. `search_history` searches the available shared mirror,
returns matching verbatim text with nearby timestamps/context, and states when
results are truncated. It does not establish native completeness. The bounded
model loop reserves its last reading round for an answer or a specific evidence
gap rather than discarding all work with a generic narrowing error.

The control worker now accepts existing room invitations without an open browser,
using the current roster space/child and exact creator/inviter identity checks.
It never changes the source sharing choice. Rejected or inaccessible threads must
be described as unavailable to the master, not nonexistent in the native app.
Regression coverage: `telegram_reading.test.js`, `control_discovery.test.js`, and
the synthetic hosted-model corrected-name/older-topic qualification.

Ask “Show my lists”, “What needs a reply?”, “Show the last 15 messages with Ada
from David’s account”, or “From David, tell Ada …”. Reply to a notification to
identify its exact account and conversation. Earlier 15 paginates the same
conversation without asking the model to reconstruct messages. A large history
can require another request while its initial index finishes loading.

History uses readable day sections, short local times, speaker names and friendly
app/account labels. A short syncing note appears when relevant; full freshness,
coverage and identity metadata remain in the authenticated API and internal routing.
Exhausting the mirror does not prove complete native history. Media retains its
exact available body (caption or filename) without interpreting unseen attachments.
Oversized history is numbered and split without losing characters.
Generated answers use the same presentation standard. They cite short quotes or
readable dates/names, never raw message or room IDs. The model is told to distinguish
inference in the specific claim (for example, “Looks like you are waiting for a
reply”) instead of adding a blanket AI disclaimer. This does not guarantee factual
model output; scoped tool evidence and confirmation remain the authority.

Changes prepared in Telegram display a review and Confirm/Cancel buttons.
The entire multipart review must be accepted by Telegram before a Telegram
confirmation is allowed. History quotes and unknown quote references cannot
approve a draft. Use `/drafts`, `/confirm DRAFT-ID`, `/cancel DRAFT-ID` or
`/status DRAFT-ID` when buttons are unavailable. Pending-draft cancellation
works without the model or Matrix connectivity. Submitted batch cancellation
uses the existing durable cancellation intent and original source envelopes;
partial/unconfirmed results remain visible and can be reconciled without
resubmitting messages. Individual submitted messages cannot be recalled here. A
send review binds the sending account, exact conversation, text, current sharing
mode and a content-version hash of the known conversation context. Edits,
redactions and new backfill invalidate the review even if the latest original
event ID stays the same. Source permissions are point-read again immediately
before submission. It expires in ten minutes. If the account,
permissions or latest message changes, prepare another review. A submitted
proposal is not evidence of delivery or reading; source outcomes remain separate.

Before each queued review part is delivered, the controller checks current
room/contact sharing and account/list bindings. Monitoring also invalidates old
pending reviews after expiry or changed access. An invalid review becomes
`review_required`; its cached text is cleared and pending parts are canceled.
Already delivered Telegram messages cannot be withdrawn by this cache cleanup.
Transient verification failures also require a fresh review rather than reusing
unverified content. Batch and contact-addition checks reuse the production CRM
validators, and native dispatch retains its own final source-side checks.

In **Lists**, choose an existing outreach list or create a **Monitoring only**
list. Open **Alerts & waiting**, search shared conversations across accounts,
and add the desired results. Enable alerts and choose browser, Telegram, or both.
Incoming-only and no message preview are defaults. Enable outgoing messages or
Telegram previews explicitly. Monitoring membership never changes Direct access.
Existing outreach contact additions retain their separate Direct activation rules.

**Show waiting conversations** shows each source-verified unanswered message,
including earlier messages when a newer one has received a reply. A conversation
can be waiting in both directions. Click the person/account to open the regular
conversation viewer. Expand a message to set its deadline, snooze, resolve or
reopen it. Blank list delays mean no automatic reminders; clearing a specific
message deadline also overrides an inherited list delay. Resolve is a manual
tracking decision and does not fabricate a native reply or read receipt.
User unread and updates unseen by the master are shown separately. **Mark updates
seen** records an exact native-order checkpoint in the list journal. Seeing the
same account/conversation through one list also acknowledges overlapping lists.
Incomplete history shows a count only for available evidence, not a full total.
Delivery is unrestricted by default. **Delivery hours** optionally pauses alerts
during a quiet window or allows them only in a working window. Pick an explicit
IANA timezone, local start/end times and weekdays. Weekdays identify the window's
start day: Sunday 22:00–08:00 includes Monday morning. Start is inclusive, end
exclusive; `24:00` is permitted for the end of a full-day window. Actual instants
are evaluated in the timezone, including skipped/repeated daylight-saving hours.
These windows defer delivery only. They never hide waiting state, change native
read receipts, or extend an elapsed-time deadline.

**New-message alerts** and **Overdue reminders** can be switched independently.
An enabled list with activity alerts off can still send explicitly configured
deadline reminders. **Repeat overdue reminders** is blank by default (once per
deadline). An explicit interval repeats while the same message remains unanswered,
unresolved and unsnoozed. Intervals are anchored to the original deadline; after
an outage, missed repeats collapse to the current interval rather than replaying
a backlog. Quiet windows never implicitly enable repeats. Overlapping lists are
evaluated independently and combine matching event/deadline deliveries; only
currently eligible list names and preview permissions affect the alert.

Expand **Conversation alert settings** below a waiting conversation to override
activity mode, deadlines, delivery hours and repeats for that account/conversation
within this list. **Use list alert settings** removes these overrides. Message
deadlines/snoozes/resolution remain separately editable. All changes are ordinary
revision-checked `list.monitoring` or `list.watch` operations in the same journal.
Telegram uses the same fields through reviewed `prepare_list_change` tools;
`/settings` shows the current list-level policy.

The canonical optional fields are `activity_enabled`, `reminders_enabled`,
`repeat_ms` (null or 60,000–31,536,000,000) and `schedule` (null or
`{timezone, mode, days, start_minute, end_minute}`). `mode` is `quiet` or `working`;
days are ISO weekdays 1–7. Conversation watches also accept explicit
`waiting_us_ms`/`waiting_them_ms` overrides, including null to disable an inherited
delay. The preparation API's `inherit_monitoring:true` removes these optional
watch overrides without altering message-level tracking controls.

**Message updates** offers immediate delivery (the default), groups per
conversation, or a digest across this list. The collection window is 1–60 minutes
for conversation groups and 1–1,440 minutes for digests. Windows use fixed UTC
clock boundaries. Quiet/working hours still gate delivery; when overdue collection
windows resume, they combine instead of emitting a digest for every missed window.
Deadline reminders retain their separate deadline/repeat timing. A conversation
override can change presentation, and restoring list defaults removes it.

The canonical `presentation` field is null/default or
`{mode: 'immediate'|'grouped'|'digest', interval_ms}`. Immediate mode requires null;
other modes require their explicit interval. Model preparation, the API and the
browser form use this same field. Overlapping eligible policies choose the earliest
delivery; an event delivered immediately is never repeated in a later digest.
Only policies ready to deliver can contribute a message preview or list label.

Telegram digests contain numbered account/conversation choices and buttons. A
multi-conversation quote requires a choice before context retrieval or drafting;
the model cannot guess which route “reply” means. Single-conversation groups retain
their exact room reference. Browser digests open a picker of the included
conversations, including older notification records loaded through the authenticated
API. Choosing an entry opens the regular viewer at its exact source event. Opening
the digest does not mark the underlying messages answered or change native receipts.

Each group contains at most 50 message events and ten conversations; larger
collections split into bounded groups. The durable per-event jobs remain the
deduplication ledger. A group claims its included jobs before external delivery.
Explicit rate limits retry after backoff; an uncertain group response or interrupted
in-flight group becomes `unknown` and is not automatically resent after restart.
Telegram still makes at most ten delivery requests per flush, prioritizing command
responses over deferred alerts. Browser display/click receipts use a random,
immutable capability for exactly the included event jobs on one device. It cannot
add events or acknowledge another device; late receipts can reconcile an uncertain
delivery. These capabilities expire after 24 hours and are pruned after 48 hours
when subsequent groups are created. For individual notifications, the receipt's
24-hour lifetime starts at the first delivery attempt, so a scheduled delay does
not consume it before the browser receives the alert. Provider acceptance, browser display acknowledgment,
opening the notification, and opening a conversation remain distinct evidence.

Browser jobs retain their exact list revisions and re-read canonical rules at
delivery. Quiet jobs remain pending; scheduled browser activity has an eight-day
delivery lifetime so a weekend can cross into the next weekly window. Ordinary
browser retries retain their existing one-hour lifetime. Telegram's operational
cache retention bounds its pending jobs. Changing/removing the authorizing list
revision cancels unsent work; a different still-valid list can independently
authorize the same event. Provider acceptance still does not prove the operator
saw a popup or read a Telegram message.

Telegram settings → **Access & retention** controls hosted-model processing,
assistant/API sending mode, allowed managed accounts, automatic list notifications
and cached-content retention. Changes use revision-checked `control.settings`
operations in the canonical journal. Sensitive access settings are available
through the authenticated settings screen/API, not model tools. Read-only mode
blocks new assistant send/contact-activation drafts and confirmation of existing
ones. Account exclusions remove that account from assistant discovery/context and
invalidate its pending reviews; restoring access does not revive old reviews.
The normal managed-account sharing and native-send rules remain authoritative.

The browser Follow button edits the canonical “Followed conversations” list.
Existing follows migrate once, retaining incoming/all-message preferences and
browser cursors. Per-conversation mode overrides preserve legacy choices.
Removing an outreach contact still leaves Direct unchanged, as specified by the
existing CRM design; removing a monitoring conversation only stops that watch.

## Source of truth and authority

Contact-by tasks use `list.contact_obligation` records in the same journal, keyed
by an exact account-bound `binding_id`, with `owner_mxid`, `generation`, the current
`membership_operation`, `created_at`, `episode_at`, nullable `due_at`/`snooze_until`, and `resolved`.
No conversation or event ID is fabricated for a person who has no linked room.
The reducer refuses a different account/generation or removed/reassigned membership;
the domain verifies current shared contact identity again before review/confirmation.
Re-adding membership does not revive its earlier deadline. Dates are never inferred
from list membership or waiting defaults.

The `contact_updates` action/tool returns contact tasks separately from message
obligations. `prepare_contact_obligation` prepares a reviewed
canonical edit. “Uncontacted” here is labeled “No shared conversation linked,”
with unknown native history; absence of a shared room cannot prove no prior contact.
An explicit task remains visible if a room later appears. It is derived as contacted
only from fresh source-owned native outgoing evidence and a positive sent/delivered/
read receipt at or after task creation. Old history, a draft, ambiguous sends and
stale source reports cannot complete it. Manual resolve/snooze remains explicit.
Changing a deadline or reopening a resolved task starts a new contact-time baseline;
snoozing preserves the original due time and baseline. The source account's approval
requirement for a new conversation is unchanged.

In Lists, **Uncontacted & contact tasks** shows the exact contact, sending account,
channel, linked conversations, deadline and snooze. Set/clear dates or resolve/reopen
there. These edits use the authenticated `contact_obligation` action with current
list revision, binding ID and only `due_at`/`snooze_until`/`resolved` changes. The
controller builds and validates the canonical operation; stale list revisions or
revoked contact identities are refused. Enable list notifications and overdue
reminders under **Alerts & waiting** to receive alerts. None of these controls sends
a message or changes sharing. Existing rooms open in the regular conversation view.

Each changed task setting advances `episode_at`; snooze preserves the original
`created_at` baseline and due date. The coordinator's typed `contact_reminder`
identity hashes owner, binding, generation, baseline, episode, due and repeat slot.
Overlapping destinations use the existing primary/fallback policy, accepted/unknown
outcomes and durable deduplication. Reminders are individual; grouping/digest applies
to activity messages. Delivery hours and explicit repeat intervals still apply.

Browser enqueue accepts only the typed contact identity and policy references,
without a room, event or caller-supplied text. It never calls the waiting controller
back synchronously. At delivery, `contact_notification` checks current canonical
membership, shared contact identity, source freshness, native completion, episode,
policy revision and reminder slot. Revoked, resolved or superseded work is canceled;
temporary verification failures remain pending. Display receipts retain the same
capability-based acknowledgment and uncertain-delivery semantics as message alerts.
Clicking an alert opens the current list/contact tasks; a removed contact cannot
route to an invented room. Telegram replies stay bound to the exact account/contact.

Validation includes `contact_obligations.test.js`,
`contact_notification_routing.test.js`, `contact_notification_adapter.test.py`,
the real Synapse `--notifications` integration (including an authenticated browser
delivery callback), and `master_contact_tasks_browser.cjs` in Chromium. All transport
tests use synthetic accounts and mocked push/Telegram providers, not real messages.

`shared/model/crm.js` adds `list.monitoring`, `list.watch`, `list.obligation` and
`list.seen` to the existing
immutable, revision-checked operation journal. Memberships, policies, per-room
deadlines, snoozes and resolution references belong to the list. Telegram and
browser notification databases hold only operational state and derived caches.

Message and conversation timer edits also carry an optional `episode_at` in
`list.obligation` / `list.watch`. UI and API operation builders share
`reminderOperationPayload`; a changed snooze, resolution or existing deadline
advances the explicit cycle, while an unchanged edit preserves it. Conversation
timer edits apply to the conversation's obligations; message edits affect that
message. A later edit advances beyond both room and message cycles, even with
same-millisecond edits or a wall-clock rollback. Snoozing an inherited list deadline
copies that effective due time rather than clearing it. The due time remains
separate from the reminder cycle.

Old journal operations do not acquire cycles during replay. Legacy Telegram and
browser delivery IDs remain unchanged. New cycles have distinct typed identities
and ID-only references; delivery checks the exact cycle against current canonical
state. Accepted or uncertain outcomes remain attached to the original cycle and
are never reset. An explicit later snooze/reopen can request a new reminder without
retrying an uncertain earlier delivery. Initial identical deadlines still deduplicate
across overlapping lists; snoozing one list does not create a new cycle in another.
An older editor that omits an already stored cycle is refused and needs a refresh.

Temporary source staleness is a delivery deferral, not cancellation. The router
retains the matching canonical policies with no ready destinations while source
verification is pending. Telegram and browser outboxes keep the same stable ID,
including across restart. Recovery re-reads replies/completion before delivery;
resolved messages, removed membership, changed revisions and revoked sharing still
cancel old work. Explicit HTTP 403/404 and verified permission changes are distinct
from timeouts, server errors and unavailable freshness evidence.

A contact task with linked conversations also requires tracking to catch up to the
latest mirrored message before claiming outreach is still missing. The browser's
`contact_notification` delivery callback returns eligible `rules` separately from
`deferred_rules`; both are matched to the queued list revision and reminder slot.
Enqueue/outcome acknowledgments never call the controller back. Normal message
reminders use the current canonical `updates` projection for the same distinction.
An unattempted reminder does not expire solely because verification took longer
than the ordinary provider retry lifetime. Once an external attempt happens, the
existing bounded retry and accepted/unknown tombstone behavior remains unchanged.

Offline regression coverage includes recovery with/without a reply, revoked scope,
restart, a two-hour deferral, native tracking lag, and real authenticated browser
callbacks against the disposable Synapse stack. These tests inject the external
providers and do not send real messages or browser pushes.

`reminder_episodes.test.js` covers queued/accepted/unknown snooze cycles, reopening,
inherited deadlines, overlap, source replies, legacy IDs, restart and clock rollback.
Python adapter tests cover the new identity, delivery revalidation and display
receipts. The disposable Synapse scenario snoozes an actually queued browser
reminder through the reviewed API, verifies cancellation of the old cycle and one
accepted delivery of the new cycle. Chromium checks preserve the original due time
through snooze and clear-snooze in the real list editor.
They are not independent watch lists. Concurrent changes use the existing
revision-conflict behavior; source account consent still gates every native send.

`agents/uplink/conversation_tracking.py` reads the existing verified native
readers for shared conversations without requiring a CRM batch. It stores only
IDs, native order and direction in the source SQLite ledger, scoped by owner,
account generation, consent journal, local room and mirror destination. Native
windows extend only through exact overlapping IDs. Multipart sends are one
obligation; gaps, changed IDs, tied ordering, unmapped messages and changed bridge
epochs become reconciling. The next message in either direction answers the most
recent unanswered opposite-direction message. It also creates its own obligation,
so earlier unanswered messages remain inspectable. Later backfill rebuilds links.

The source publishes `com.jkali.conversation_tracking` as its own scoped account
only after repeating live sharing and destination checks. This derived evidence
is not a watch list. The master derives account-scoped read-work hints from the
canonical lists and publishes `com.jkali.monitoring_scope` in each account's
verified proposal room. These hints contain only room IDs and generation, never
deadline/alert rules; they expire after five minutes. The source verifies the
manager author and current proposal-room binding, then intersects requested IDs
with its live shared mirrors. It visits one selected mirror per pass, at most
once every five seconds; native reads have their existing timeout. Unlisted
conversations do not consume this scan. Very large selected lists can still have
stale projections until subsequent passes. The retained ledger is capped at 10,000
native entries, and published state at 250 messages / 48 KB. Limits produce
reconciling status, never silently discard outstanding obligations. These bounds
need operational review before scaling to large histories.

The master verifies source ownership, account generation, room, native event
order and exact mirrored IDs. A newer mirrored message than the native projection
suppresses reminders until the projection catches up. Native EOF, a complete
recent suffix and observed bridge history are distinct coverage labels; an empty
bridge history cannot establish that a native conversation was never contacted.
Exact per-message receipt state is separate from response links. Unsupported or
missing delivery/read evidence remains unknown.

`master/control-domain.mjs` reuses the production CRM store, membership adapter,
batch adapter and source-account verifier. Discovery is restricted to the
configured roster, source-authored child links, source-owned shared mirrors and
source-owned shared-contact catalogs. Exact profile/grant identities connect
outreach membership to conversations; names never grant routing authority.

Production discovery uses a process-local full-state cache with a fresh Matrix
sync barrier on every refresh. Startup requests only a stream position, then
reads the roster-authorized spaces and children. Subsequent sync requests include
all rooms and unfiltered timeline event types (one event per changed room is
enough for invalidation); redundant sync state payloads are excluded. Any room
reported in join/leave/invite/knock invalidates its cache entry. Changed room
authorization is read from the full-state endpoint, never reconstructed from a
limited sync timeline. This follows the incremental token semantics of the
[Matrix sync API](https://spec.matrix.org/v1.15/client-server-api/#get_matrixclientv3sync).

Sync failure or malformed responses clear both cache and token and fail the
refresh. Removed links/account scope prune cached state. Explicit 403/404 room
refusals are retained for at most one minute and invalidated immediately by room
deltas; they never grant access. Network/5xx errors are retried on the next refresh.
Point-of-use permissions, proposal validation and exact history reads remain live.
No cache or sync token is restored after restart. `status.discovery` reports
counts and duration only, with no room IDs or content. On this installation's
1,253 shared conversations, standalone refresh measured about 7.8 seconds cold
and 39–49 ms warm (three requests instead of 1,687). These are observed measurements,
not a guaranteed latency bound; queued work and source activity can increase them.

### Backup and restore

`master/lifecycle.py backup` includes exactly `control.local/{control.db,config.json,enabled}`
and `notifications.local/{notifications.db,vapid.pem}`. SQLite's backup API includes
committed WAL records and produces normalized databases; logs and live sidecars
are excluded. Configuration, provider credentials, VAPID keys, pairing and cached
conversation material are private backup contents. Keep the complete backup
directory private (0700; files 0600), including when copying it to another host.
Canonical list policies and obligations remain in the Synapse database dump.

Before taking a snapshot or restoring, lifecycle maintenance records and stops
the control worker, enrollment/browser writer and master Synapse. It checks the
worker's immutable container ID and `/state` mount against this installation;
a different installation's container causes a refusal. Resume starts only the
same worker if it was originally running. Archive rebuild also stops that worker
while retiring mirrors. Teammate stacks and native send ledgers are outside this
operation's scope. Never exercise restore against production as a test.

Recovery preserves a current local feature directory when its database survives,
so newer accepted/unknown outcomes, pairing revocations, browser subscriptions and
provider keys cannot be overwritten by an older snapshot. This retention decision
is persisted before copying so an interrupted retry cannot mistake restored old
state for newer evidence. If only the backup survives, the restored bot requires
fresh pairing and restored browser devices are disabled until reconnection.

In either recovery path, unattempted alerts and API jobs are canceled; in-flight
attempts become unknown; pending reviews require a new review. Accepted, submitted
and uncertain evidence remains. Cursors restart observation without replaying
historical alerts, and routing attempt tombstones remain. The existing browser
dispatcher handoff identity survives, but its frozen old cursors are cleared.
After Matrix authentication is sanitized and accounts are reprovisioned, lifecycle
rewrites the worker's manager credentials and roster without replacing its
Telegram/OpenRouter credentials. Restore is not an instruction to retry sends.

From the selected installation root, operational commands are:

```sh
python3 master/lifecycle.py --root /path/to/installation backup --output /private/new-backup-directory
python3 master/lifecycle.py --root /path/to/installation restore --backup /private/new-backup-directory --confirm-restore
```

Restore disables tailnet ingress; verify locally before running
`master/tailscale-serve.sh`. An interrupted restore leaves maintenance active:
fix the reported error and repeat the same restore command. If no worker container
exists, `control_setup_required` tells the operator to install the worker for the
selected master state. Confirm that an existing `beepa-control` belongs to this
installation before invoking the installer. Older backups lacking these feature
files cannot reconstruct them; surviving current state is still retained.

`control_recovery.test.py` exercises actual schemas and production core startup,
including zero replay, stale pairing quarantine, interrupted copying and WAL
snapshots. `master_lifecycle.test.py` covers operation ordering and foreign/replaced
worker refusal. `tests/integration/run.sh --lifecycle` drills full database restore
and snapshot-only recovery on marked disposable Synapse stacks, with a network-
disabled Node 22 control fixture and synthetic notification state. It verifies
current outcomes, refreshed credentials, required re-pairing/reconnection and
preserved teammate/native sentinels. It does not deliver a live notification.

History indexes only source-authored messages, handles edits and redactions,
and uses original timestamps and verified timestamp corrections. It reads the
mirror stream to its end before claiming a complete recent window, then advances
incrementally. Cursors bind the room and exact preceding message. Revocation
discards its in-memory history. There is a 100,000-message per-conversation limit;
oversized or incomplete histories fail visibly rather than inventing an answer.

The only individual send is a typed `com.jkali.proposal` in the verified source
proposal room. Direct writes `com.jkali.send_mode: direct`; Share writes
`suggest`. The existing uplink/user dispatchers own transmission and approval.
CRM batches reuse immutable manifests, selected account routes and source-user
approval for new conversations. Telegram never calls a native bridge send API.
Ambiguous route selection is refused; `batch_routes` supplies verified choices
for the operator to disambiguate before preparing a batch. Archival uses the existing cancellation-aware membership adapter.

List editing is available through the same reviewed tools in Telegram and the
authenticated API. `prepare_list_change` creates a monitoring list by default;
`purpose: outreach` explicitly creates an outreach list, with a separate review
before contacts are added and Direct is enabled. `lists(include_archived: true)`
finds archived lists and `prepare_list_change(operation: restore)` restores their
saved memberships/settings without replaying canceled setup or sends.
`prepare_member_removal` requires an exact list and account-bound binding. Its
review pins the membership operation, binding fingerprint and list revision.
Confirmation uses `createCRMMembers.applyChange`, including the source-account
cancellation handshake; it never silently switches to another sender. Existing
Direct remains enabled, other memberships and sent history remain unchanged.

The `settings` tool reads current global settings. `prepare_global_notifications`
reviews enabling/disabling automatic alerts across all lists and destinations.
It preserves individual list policies and every sensitive access setting. Account,
model-context, sending-access and retention changes remain restricted to the
authenticated master settings UI/API. `/settings` states the global alert switch
separately from each list's policy, including when there are no lists.

`master/control-core.mjs` provides single-use pairing, exact operator checks,
durable inbox/outbox, reviewed drafts, response references and monitoring.
The model has preparation tools but no confirmation tool. A callback must match
the reviewed draft reference. Concurrent confirmations claim one draft once.
An interrupted submission or possibly delivered Telegram request becomes
`unknown`; it is never automatically resent. Status may reconcile an individual
proposal using its source-room/manager-authored `com.jkali.control_draft_id`, exact
immutable payload and unique event identity. This is a read-only lookup, not a
retry. Conflicting identities remain uncertain; matching text is insufficient. Explicit Telegram rate limits get
bounded retries. Unknown Telegram deliveries can consequently miss an alert;
avoiding duplicates after ambiguous network failures takes precedence.

New-message monitoring starts at the current cursor, skips old backlog and
deduplicates across overlapping lists. Policy changes or lost room access cancel
queued Telegram alerts. Reminders require an explicit deadline, a caught-up
stream and a connected uplink with a recent health report and no history/delivery
errors. The freshness allowance is seven minutes because healthy uplinks publish
unchanged health every five minutes. Both channels deduplicate an identical
conversation/message/deadline across lists. Browser push acceptance and browser
display acknowledgment remain separate evidence.

## Model and credentials

No earlier internal model was configured. This installation uses OpenRouter
`deepseek/deepseek-v4.1-flash`; its tool call was checked against the live provider.
It was the cheaper eligible interactive DeepSeek/Luna model in the catalog when
configured. Pricing/model availability can change; there is no silent runtime
switch to a more expensive model. Change `BEEPA_LLM_MODEL` and rerun setup to
select another tool-capable model.

Only relevant requested list/conversation data is supplied through tools. Contact
and message text is untrusted input. Configured credentials are never model context.
Before every hosted request, text and structured fields are scrubbed for common
credential formats: provider API/bot tokens, Matrix/GitHub/Slack tokens, AWS key
IDs, JWTs, private-key blocks, authorization/cookie headers, URL passwords and
labeled password/key/token/secret values. Scrubbing precedes truncation and also
applies to retained dialogue frames. This is pattern-based masking, not a promise
to recognize every arbitrary secret hidden in prose. Literal authorized message
replay bypasses the hosted model and remains exact. A model draft containing the
mask marker is rejected; sensitive outgoing text can be completed in the master
composer or authenticated typed API without reconstructing credentials in the model.

Queued generated answers, list/settings displays and literal history carry an
ID/hash-only source scope. Before each part, the worker refreshes canonical access
and source sharing, checks the captured scope, and checks history versions used
by the answer. Canonical/access or relevant history changes cancel the remaining
parts, remove cached text/references, and queue a generic request to ask again.
Accepted part outcomes remain recorded; already delivered Telegram content cannot
be recalled by this check. Temporary verification failures defer delivery. The
scope survives restart. Model rounds are also rechecked before more context is
processed or tools run, and stale dialogue context is discarded. Scope matching
is deliberately conservative: even an unrelated canonical edit can require a new
answer. It must not be weakened into cached permission checks to reduce latency.
On the first upgrade, pending legacy answers without a stored scope are retired
and their cached text erased. Existing validated alert/review jobs and accepted or
uncertain delivery outcomes are retained; native-send records are untouched.
`master/.env` contains `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, and optional
`BEEPA_LLM_MODEL`. It and `master/control.local/config.json` are mode 0600 and
Git ignored. Do not paste their values into command arguments, documentation,
logs or commits. The worker gets the manager-scoped credential, not source-user
or native-account credentials.

## Install, restart and maintenance

Prerequisites: the master stack and enrollment service are running, Docker is
available, and the locally installed `node:22-alpine` image supports `node:sqlite`.
Run `python3 master/setup-control.py`. It resolves the local image content ID,
starts `beepa-control`, checks health and enables canonical browser-list
integration. Restart the enrollment service once after first enablement.

The container publishes only `127.0.0.1:8023`, uses a read-only filesystem,
unprivileged host UID, dropped capabilities and selected source-file mounts.
Its restart policy is `unless-stopped`. CRM discovery and Telegram startup retry
independently. Authenticated list, rule, settings and domain APIs work while
Telegram is unavailable; they still require fresh authorized source reads.
Monitoring continues, while Telegram replies wait for verified transport recovery.
Pairing, Telegram commands and test alerts require successful bot identity/webhook
checks. A command-menu setup failure does not disable text commands. Status exposes
`domain_ready` and `transport_ready`; `ready` means both. Status reads do not
queue behind model calls. Telegram uses outbound long polling;
no public webhook is needed. An existing webhook is refused, not deleted.
Only one process should poll this bot token. Docker and this Mac must be running
for Telegram alerts; a closed browser is supported by Web Push, while an offline
Mac cannot run the local bot worker.

Health reports master startup, Telegram setup, polling, delivery, monitoring, model
requests and command processing separately, with check/success timestamps.
Monitoring success cannot clear a Telegram failure. Polling becomes visibly stale
after 90 seconds and monitoring after seven minutes. A paired account is distinct
from a healthy transport. Delivery outcome counts retain rejected and uncertain
results even after a later message succeeds.

`master/control.local/control.db` stores pairing, processed Telegram updates,
pending drafts, outbox outcomes and message references. Preserve it across
restarts. The updater retains this directory, snapshots SQLite with its backup
API, and restarts the worker from the selected release. Do not restore an old
send database over newer outcomes. A regenerated worker config refreshes the
configured roster, so rerun setup after adding/removing a managed account.
Source sharing revocations are rechecked continuously without a setup rerun.

The verified numeric Telegram bot ID binds transport state. A replacement token
for that same ID preserves pairing, pending replies, update offsets and references.
A different ID atomically clears the pairing/challenge and message references,
cancels unsubmitted reviews and retires queued replies. It requires a new private
pairing link. Minimal per-bot input and delivery tombstones plus update offsets
prevent replay if a previous bot is later reinstalled. Status exposes retired
delivery outcomes separately. Native proposal results, uncertain sends, API request
deduplication and source observation cursors are preserved. They are never reset
to repair Telegram. Legacy databases without a verified bot binding require fresh
pairing unless an operator explicitly verifies and adopts the existing identity
before upgrade; matching a display name or merely editing configuration is not
identity proof. No former bot's pairing is restored automatically.

Default cached-content retention is 30 days, configurable from 1 to 365 days.
Maintenance runs after a successful monitoring refresh. Old inbound command text,
outbox text, quote references and closed/non-batch submitted draft bodies are
cleared. Request IDs, outcome/proposal IDs and deduplication tombstones remain, so
old callbacks and retries cannot execute again. Uncertain sends and submitted
batch manifests retain their immutable payloads for reconciliation/cancellation.
Inactive in-memory history caches expire after one hour and are rebuilt from
authorized source history when needed. SQLite secure deletion is enabled and
maintenance checkpoints its WAL. This does not delete original conversations,
already delivered Telegram messages, provider copies or separately retained
backups; it is the retention policy for this controller's operational cache.

Enrollment now serves concurrent requests so a control request can call back
into CRM verification. Enrollment redemption and roster/CRM mutations still
share a serialization lock; notification/control requests have their own
worker serialization. This avoids a proxy/CRM callback deadlock without making
single-use enrollment redemption concurrent.

## API

All operations use `POST /admin/control` with the current master session bearer.
On this Mac the API base is `http://127.0.0.1:8019`; port 8011 serves the legacy
static console, which already selects port 8019 for its API calls. The dedicated
master gateway uses its own same-origin API path.
The enrollment service authenticates the manager before parsing the body; the
loopback worker independently verifies the manager. The endpoint does not
accept a user ID as authorization. `apps/master/control.js` is the only newly
allowlisted browser writer to this endpoint.

| Action | Purpose |
| --- | --- |
| `status` | Pairing, model, polling error, pending count, draft states and latest ten API command jobs |
| `settings` / `settings_update`, `revision`, `settings` | Read or revision-check basic model/access/retention settings in the canonical journal |
| `pair` / `disconnect` | Mint a 15-minute private link or revoke pairing |
| `test`, `request_id` | Idempotent labeled alert to the paired operator |
| `lists`, optional `include_archived` / `rules` | Read canonical lists or effective monitoring rules |
| `tool: prepare_list_change` | Review create (monitoring/outreach), rename, archive, restore, monitoring or conversation-watch changes |
| `tool: prepare_member_removal` | Review removing an exact list/binding through source cancellation; preserve existing Direct |
| `tool: settings` / `tool: prepare_global_notifications` | Read settings or review the global automatic-alert switch; no sensitive access mutation tool |
| `updates`, optional `list_id`, `room_id`, `owner`, `source`, `since` | Scoped waiting, unread/unseen, messages, deadlines and freshness; `since` is UTC milliseconds |
| `tool: prepare_obligation` | Review deadline, snooze or resolution for an exact list/room/message |
| `tool: prepare_seen` | Review acknowledging master updates for an exact list/conversation |
| `tool: person_context` | Exact shared profile bindings and latest messages for a canonical person ID; optional exact owner |
| `tool`, `name`, `arguments` | Invoke the same scoped read/preparation tools |
| `confirm`, `id` | Confirm the exact prepared draft |
| `cancel`, `id` / `draft_status`, `id` | Cancel/reconcile unsent work or read source outcomes |
| `command`, `text`, `request_id`, optional `session_id` | Durably queue a command and return immediately; replies arrive in Telegram |
| `command_status`, `request_id` | Read processing state, reply delivery outcomes and associated draft IDs/states |
| `command_cancel`, `request_id` | Cancel an API command that has not started; cannot recall an executing send |
| `follow` / `unfollow`, `room` | Edit the canonical legacy-follow list |

The command API requires a unique request ID and acknowledges durable storage
with HTTP 200 and `state: queued`; it does not wait for the model or delivery.
`command_status` reports `queued`, `processing`, `processed`, `failed`, `unknown`,
`canceled` or `expired`. `processed` means command execution ended successfully,
not that Telegram accepted the reply or that a native message was delivered.
Use the separate delivery counts and associated draft/source status. Status,
submission and queued-command cancellation bypass the domain execution queue but
still authenticate the master through Synapse. Domain operations remain serialized.
The command consumer releases its execution lease only while awaiting the hosted
model. List/rule reads, canonical settings changes and monitoring can proceed in
that interval. The returned model response must reacquire a FIFO lease, then pass
fresh context/history, hosted-access and operator-identity checks before any tool,
draft or reply. Only one command consumer runs; there are no concurrent model
commands or overlapping domain mutations. Disconnect increments an identity epoch,
so reconnecting the same operator cannot revive an answer from the old session.
Bot rotation also prevents the old continuation from completing a reused Telegram
update ID in the replacement bot's inbox. A provider error reacquires/releases the
lease too, so it cannot strand subsequent work. Slow source calls still serialize;
this change removes provider latency, not every possible wait.

Retry the exact same request ID, text and session after a lost HTTP response.
The server returns the existing job; it rejects different text/session for that
ID. Failed, interrupted and completed requests never rerun with the same ID.
Restart resumes jobs that had not started; an interrupted processing job becomes
`unknown`, with its input erased and no automatic replay. Queued inputs are bound
to the paired operator, connection epoch and numeric bot identity; disconnect or a different bot
cancels them. Completed/error/canceled job inputs are erased immediately, while
request hashes, outcomes and draft IDs remain. Queued inputs expire under the
configured content-retention period. At most 100 API commands can wait at once.

Telegram polling now writes incoming updates and advances its durable offset
independently of slow command processing. A single consumer processes the oldest
pending API or Telegram work, preserving order and existing confirmation rules.
The browser’s Ask Beepa form uses this same API, keeps the same request ID when
retrying a lost response, and shows processing separately from reply delivery.
It can be closed without canceling a saved job. Its API session is separate from
Telegram dialogue context; reuse `session_id` for headless followups too.

Inspect status and prepare a new request only after checking existing drafts and
outcomes. `test` confirms queue processing, not that
the operator read a message. Source-user approval remains necessary where the
existing sending path requires it.

Telegram keeps up to three recent exchanges in memory for ten minutes, bounded
to 32,000 serialized characters. It retains exact tool-returned account/room
identifiers and displayed options, so a reply such as “the second one” can resolve
the previous question. Every new command still refreshes authorized discovery.
A changed canonical/access fingerprint discards previous exchanges before a
model request. Quoted alerts take precedence over this conversational context;
unknown quotes never fall back to a remembered conversation. Old messages are
context, not current source evidence: preparing a send still reads and binds the
latest source context and requires a complete exact review.

Only fully accepted Telegram replies enter usable dialogue history. A bare
“yes” after an ordinary answer or clarification cannot approve an unrelated
pending draft. Use its exact review/button or `/confirm DRAFT-ID`. `/new` clears
conversation memory without canceling pending drafts. Restart, disconnect,
disabled model access and expiry clear memory; it is never written to SQLite.
API commands have no implicit shared dialogue. An optional `session_id` (1–100
letters, digits, `_` or `-`) enables API followups separately from Telegram and
other session IDs. The process holds at most twenty sessions; oversized complete
exchanges are discarded rather than retaining truncated candidate mappings.

## Validation

`tests/run.sh` passed all discovered unit checks and consent/invite conformance.
Targeted reruns cover subsequent control/history/UI changes.

`tests/unit/telegram_control.test.js` checks pairing expiry and isolation,
model non-confirmation, concurrent confirmations, stale permissions, restart
ambiguity, alert dedup/revocation, overlapping lists, original/edited/redacted
history, pagination, late backfill, policy validation and revisions.
`telegram_confirmation.test.js` exercises quoted/unknown reference isolation,
complete multipart reviews, edited and revoked context, strict tool schemas,
deterministic controls, durable batch cancellation and exact-ID reconciliation.
`telegram_operational.test.js` exercises independent health/error recovery,
staleness, blocked delivery, contact/batch revocation between multipart reviews,
cached text cleanup and rejection of old confirmations without native sends.
`telegram_identity.test.js` covers same-bot token rotation, changed-bot pairing
isolation, returning-bot deduplication and preservation of native-send records.
`telegram_service.test.js` exercises the actual authenticated HTTP runtime through
startup failures, an existing webhook, lost connectivity and transport recovery.
`telegram_async.test.js` holds a model call open while submitting and inspecting
jobs through HTTP and ingesting Telegram updates. It covers exact-ID retry,
queue cancellation, restart uncertainty, bot isolation, retained outcome/draft
associations and retention. Browser coverage includes a lost submission response,
same-ID retry, canceling queued work and distinct processing/delivery labels.
`telegram_context_privacy.test.js` covers unquoted queued summaries, changes during
multipart delivery or model calls, contact/account/message revocation, deferred
verification, restart, credential masking and exact authorized history replay.
The disposable Synapse scenario also verifies canonical list access and scoped
authentication through this runtime while Telegram is unavailable, plus an actual
source sharing revocation after a model summary was queued.
`control_settings.test.js` verifies canonical revisions/replay, account filtering,
hosted-model and read-only gates, retention and restart-safe duplicate protection.
`telegram_followups.test.js` verifies account choices, quoted-route precedence,
scope revocation, cache expiry/bounds, failed delivery, API-session isolation and
clarification-versus-confirmation behavior. The real Synapse scenario also checks
that refreshed discovery preserves valid context and access changes discard it.
`telegram_list_editing.test.js` verifies explicit list purposes, archived discovery
and restore, account-bound removal, cancellation dispatch, reassignment/re-addition
and access-change refusals, expired reviews, global-alert revision conflicts and
sensitive-field rejection. The disposable Synapse client also performs these edits
through authenticated HTTP, uses a source-authored cancellation acknowledgment,
and verifies repeated confirmation does not issue another cancellation or removal.
`monitoring_schedule.test.js` tests canonical schedule validation, DST boundaries,
overnight windows, quiet deferral, repeat intervals, independent activity/reminder
switches and overlapping policies. Its Python counterpart compares more than
1,800 schedule cases with the JS evaluator. Browser worker tests cover durable
deferral, answered-message cancellation, overlap and revision changes. The real
Synapse scenario round-trips schedule/override operations, defers an alert, and
opens its window using an injected clock. Chromium tests save schedules and
conversation overrides through actual form controls and restore list defaults.
`notification_groups.test.js` and its Python counterpart verify collection windows,
overlap, per-account digest routes, bounded groups, source revocation, retry timing,
crash/uncertain outcomes, callback authorization and device-scoped receipts. Both
evaluators share parity cases for presentation windows and invalid policies. The
real Synapse test persists a digest policy and combines two exact source events;
Chromium exercises the digest picker across two accounts, older record retrieval,
the actual notification worker, and its receipt endpoint under production CSP.
`conversation_native_evidence.test.py`, `conversation_tracking.test.py` and
`telegram_obligations.test.js` cover source read contracts, exact overlap,
message-level responses in both directions, scoped publication, receipt
separation, canonical per-message changes and reminder cancellation/deduplication.
`master_control_api.test.py` checks authentication-before-body and error redaction.
`master_notifications.test.py` checks one-time canonical migration, failure
suppression, explicit healthy deadlines and canceled pending outgoing alerts.

`tests/integration/run.sh --notifications` passes against generated disposable
Synapse stacks. Its headless production control client exercises real journal
writes/replay, exact history, incoming alerts, deduplication, a reviewed typed
Direct proposal and refusal after an intervening message. Synthetic source
accounts have no real native transport.

`master_control_browser.cjs` and `master_notifications_browser.cjs` pass in
Chromium with fully intercepted synthetic services: list settings, no default
deadlines, inline watch search, normal conversation navigation, Telegram test,
existing browser permission/subscription flows and narrow mobile layout.

Live evidence on this Mac: private pairing succeeded; Telegram accepted the
connection reply, labeled test and an API-triggered natural-language list reply.
Browser notification service remained connected with no reported polling error.
No native messages were sent during those initial transport tests. The later
controlled self-send is recorded below. Telegram acceptance
does not prove the recipient opened the reply, and this test does not replace
native-provider receipt tests.


## Requirement evidence audit — 2026-10-18

This maps the original WhatsApp design's full acceptance scope to the Telegram
implementation. Telegram replaces the control transport only; account authority,
canonical lists, context, confirmation and monitoring requirements remain. The
final model qualification (`pm_mng-ebs.11`) passed: requested reminders produce
a review for the correct message and date, with server-side ISO conversion. The
scheduler/API behavior is independently verified below. This evidence is bounded
by the documented source coverage and device-display limits; model qualification
is a regression sample, not a guarantee for every natural-language phrasing.

| Original requirement | Implementation and inspected evidence |
| --- | --- |
| Central lists across accounts | `ControlDomain.listRooms/updates`, `control_cross_account.test.js`: one monitoring list contains Ada under David/iMessage and Elliot/WhatsApp, with separate histories, obligations, unread and master-seen values. Outreach bindings remain account-specific; a canonical person has one primary binding per outreach list, while other accounts can belong to other lists or be watched in the same monitoring list. |
| Cross-app lists | `control_cross_account.test.js`, `telegram_obligations.test.js`: unfiltered updates include both apps; owner/source filters select exact routes without changing them. |
| Exact last 15 and more | `ControlDomain.history`, `enqueueHistory`, `telegram_context_replay.test.js`, `telegram_control.test.js`: exact chronological text, stable cursor with new arrivals, edits/redactions/backfill, captions, availability labels and lossless numbered continuation. |
| Person context | `personContext`, response-scope validation, `telegram_obligations.test.js`, `telegram_context_privacy.test.js`: current shared bindings only, exact owner filter, revocation before model/tool/delivery, credential masking, readable source references and inference guidance. |
| Individual alert replies | `ControlCore.ref/command`, `telegram_confirmation.test.js`, `telegram_followups.test.js`, `notification_groups.test.js`: durable exact route references, quote precedence, unknown-quote refusal, numbered digest choices and isolated conversation sessions. |
| Sending confirmation | `prepareSend/validateSend/confirm`, `telegram_confirmation.test.js`: exact body/account/channel review, complete multipart delivery, ten-minute expiry, changed history/permissions/generation refusal, multiple-draft ambiguity and exact-ID reconciliation. No model confirmation tool exists. |
| Direct and Share | `control_cross_account.test.js`, `master_suggestions.test.js`, source direct-send and proposal-decision tests: Direct submits after master confirmation; Share retains separate source-user decisions. Explicit Direct refusal never becomes an actionable suggestion. New conversations use reviewed source approval. Live self-send below verifies the Direct path. |
| Batch sending | `crm_batches.test.js`, `crm_batch.test.py`, adversarial batch tests and `telegram_confirmation.test.js`: frozen personalized manifest, per-account children, per-item outcomes, cancellation, repeated confirmation and uncertain-submission reconciliation. |
| Watch versus CRM authority | `telegram_control.test.js`, `telegram_list_editing.test.js`, CRM member/control tests: a watch only writes canonical monitoring metadata; explicit outreach additions preview Direct activation, including Private conversations. Removal cancels pending activation and preserves existing Direct. |
| Waiting and replies | `conversation_native_evidence.test.py`, `conversation_tracking.test.py`, `telegram_obligations.test.js`: source-owned native ordering, latest unanswered opposite-direction attribution, earlier obligations retained, scoped publication without bot commands, gaps/ties/old generations refused. |
| Hanging reminders | Schedule parity tests, `reminder_episodes.test.js`, contact-obligation/routing/adapter tests: no implicit deadline/repeat/quiet hours; both directions, contact-by tasks, explicit snooze/reopen cycles, preserved due times, resolution and timezone/DST handling. |
| Evidence and unread | `telegram_obligations.test.js`, receipt/source tests and cross-account acceptance: native send/delivered/read, response, source unread and master unseen stay separate. Unknown and stale evidence are labeled, never inferred from a reply. |
| Offline recovery | `reminder_episodes.test.js`, notification adapters/groups/dispatcher tests: fresh catch-up before reminding, backlog baseline, durable pending deferral, replied/removed cancellation, bounded provider retries and no resend of accepted/uncertain work. Real browser delivery callbacks exercise source disconnect/reconnect. |
| Transport constraints | Telegram-specific chunking, pairing, blocked-operator and retry tests; `notification_routing.test.js` and adapter tests prove definitive-failure fallback and no uncertain-delivery fallback. WhatsApp templates/windows are not part of the chosen Telegram transport. |
| Authorization | Actual HTTP API tests, identity/discovery/privacy/confirmation tests and disposable Synapse source revocation: one paired private operator, manager authentication before body parsing, source-owned state, prompt content treated as data, no model send/confirm authority, obsolete generations and forged references refused. |
| Settings and API parity | `control_settings.test.js`, `telegram_list_editing.test.js`, schedule tests and Chromium control fixture: canonical list edits, overrides, activity/reminder switches, presentation, destinations/fallback, explicit timers and global alert control. Sensitive model/account/sending access, identity and retention use authenticated settings/API rather than conversational mutation. |
| Single source of truth | Shared CRM reducer/store, one-time follow migration, notification dispatcher/routing tests and real Synapse journal replay: policies and memberships converge on canonical revisions; adapter databases contain derived cursors/jobs/outcomes only; overlapping policies deduplicate per destination. |
| Maintenance | `control_recovery.test.py`, disposable lifecycle restore, async/operational/identity/storage tests: consistent SQLite snapshots, preserved newer outcomes, ambiguous in-flight quarantine, re-review/re-pair after snapshot-only disaster recovery, independent source/transport/model health and responsive API during model waits. |

### Controlled live evidence

On 2026-10-18, the authenticated master API prepared and confirmed one clearly
labeled iMessage to the operator's previously approved self-chat. The review was
checked against the exact mapped native room, source account and body before
confirmation. The source recorded a non-actionable auto-sent artifact; the native
daemon confirmed its exact outgoing event/engine ID. Native delivered/read evidence
and the source-owned master receipt both became positive. No additional recipient
was contacted, no consent setting changed, and no ambiguous send was retried.
The private exact-ID record is `master/control.local/acceptance/reviewed-self-send.json`
(mode 0600, Git-ignored). This self-chat result does not promise read support from
another person/device/provider that does not publish native receipts.

The passive propagation probe reported healthy source ingestion and no pending
uplink events/errors before this test. Telegram remains privately paired; browser
service reports one enabled subscription. Earlier display receipts prove browser
worker display acknowledgment. An independently observed macOS banner remains a
separate device/OS check; provider acceptance alone is not evidence of a popup.

Automated sources are synthetic and isolated: the full discovered unit/conformance
suite, disposable Synapse notification/lifecycle suites, and intercepted Chromium
fixtures cannot send to real native recipients. The optional hosted-model
qualification sends only fictional fixture context to the configured provider and
uses a synthetic Telegram adapter and in-memory canonical store. It does not load
production conversations, run production tools, or confirm a send.


The opt-in model check is `tests/qualification/telegram_model.mjs --live-model
/absolute/private/config.json` (Node 22 with `--experimental-default-type=module`).
It sends only synthetic fixtures to the configured OpenRouter model, caps requests,
and reports each scenario separately: account ambiguity/followup, exact Direct
review, outreach-list purpose, latest-message deadline, independent activity/reminder
switches and cross-account waiting. A final optional case-name substring narrows a
rerun; an empty selection fails. This is separate from disposable Synapse integration
and never executes production domain/Telegram/native tools.

`history` explicitly documents that display mode ends a request. Internal context
reads for drafting, summaries, timers or list edits must use `display=false` and
continue to the requested review. `updates` accepts an exact `room_id` through both
the model tool and authenticated API, avoiding an unrelated conversation's context.

Deadline preparation accepts exact UTC ISO dates (`2026-10-19T12:00:00Z`, optional
three-digit milliseconds), canonical integer milliseconds, or null to clear.
Models are instructed to use ISO dates; the server performs timestamp conversion
and rejects impossible calendar dates or missing timezone markers. Relative dates
are resolved against the current clock supplied to the model. Ambiguous local
timezones require clarification. Reviews show a human-readable UTC deadline in
addition to the exact canonical payload, and still require operator confirmation.
This avoids asking a language model to calculate Unix timestamps. Duration fields
remain integer milliseconds; canonical list storage is unchanged.

### Final local verification — 2026-10-18

- `tests/run.sh`: all discovered unit and conformance checks passed
  (`/tmp/beepa-iso-final-checks.log`). The new cross-account/date regression also
  passed in production Node 22.
- `tests/integration/run.sh --notifications`: disposable real Synapse, authenticated
  controller/browser callbacks and canonical ISO-deadline persistence passed
  (`/tmp/beepa-iso-final-integration.log`). Earlier disposable lifecycle recovery
  and current Chromium control, notification and contact-task suites passed.
- Hosted qualification: all six scenarios passed using the configured
  `deepseek/deepseek-v4.1-flash`, 18 provider calls, synthetic fixtures only and
  zero actual sends (`/tmp/beepa-model-iso-qualification.log`). Individual calls
  took approximately 4–29 seconds; a multi-step command can take longer.
- Restarted `beepa-control` with mounted module hashes matching the workspace.
  Authenticated runtime checks verified paired/ready/routing-ready, no pending
  replies or health errors, polling/monitoring success after startup, exact-room
  filtering and rejection of an impossible ISO calendar date. The enrollment
  proxy masks detailed refused-request errors; the private worker's authenticated
  response verified the date-specific rejection. One browser remained connected.
- `git diff --check` passed. No commit, Git push or Dolt remote synchronization
  was performed. The macOS visual-banner observation remains a separate device
  check tracked by `pm_mng-2pu`; browser display acknowledgments are recorded.

### Readable Telegram conversation history — 2026-10-18

Normal history replies show the conversation name and app, the sending account's
display name, a date heading once per local calendar day, and short times beside
speaker names. Matrix event IDs/account handles, ISO timestamp dumps, message
counts and sync diagnostics are omitted. A stale source gets only “Recent messages
may still be syncing.” Exact message bodies, ordering, captions and links are
preserved. Pagination and reply routing keep their exact IDs and validation scope
internally; the authenticated history result still contains full availability
metadata. Display simplification never implies native history completeness.

The worker config `timezone` controls history formatting; `setup-control.py`
preserves it, supports `BEEPA_TIMEZONE`, and otherwise uses the installation host's
IANA timezone (UTC if unavailable). This installation uses America/Los_Angeles.
Optional `account_names` maps exact account IDs to display names; it has no effect
on account authority. Without an override, use the account's own space membership
display name, then a readable account label. This installation maps its jkali
account to Jacob. These private presentation settings survive setup reruns.

Validation includes local midnight and DST changes, multiple messages under one
date, display names, hidden IDs with intact routing references, exact multiline
bodies and multipart pagination, on host Node and production Node 22. Existing
scope revocation and confirmation checks remain in the full suite; isolated real
Synapse exercises history rendering and API integration without native sends.


### Formatting generated Telegram answers

Generated answers convert common Markdown headings, bold, italic, inline code and
fenced code into Telegram `entities`. Plain message text is sent without a parse
mode, so HTML-like source text and punctuation cannot become executable markup or
cause MarkdownV2 escaping errors. Entity offsets use UTF-16 units, including emoji;
long replies split text without splitting surrogate pairs and clip formatting
spans to each numbered part. Unsupported Markdown remains readable literal text.
This renderer is opt-in for generated answers only: exact history replay, outgoing
message reviews and stored send bodies remain literal. It does not alter routing,
confirmation, queue retries or source-scope validation.

Protocol reference: https://core.telegram.org/bots/api#messageentity
Regression: `tests/unit/telegram_formatting.test.js` exercises actual generated
reply delivery, literal reviews, malformed markup, code, emoji and multipart spans.


### Shared presentation standard across bot messages — 2026-10-18

Summaries, direct alerts, contact/message reminders, grouped digests, send/batch
reviews, list settings, review outcomes, status and errors use the same account
names, app labels and local date/time presentation. No default reply displays raw
Matrix/event/draft IDs or a JSON policy dump. The generated-answer prompt uses the
same standard, with preformatted local times in tool results to avoid model time
conversion. A deterministic display filter additionally removes protocol IDs and
formats explicit UTC timestamps in generated/administrative text.

Exact history, original message previews and outgoing send bodies bypass metadata
cleanup and remain unchanged. IDs stay in stored drafts, scope checks and callback
references. `/drafts` offers human-readable request cards with status/cancel buttons;
status callbacks verify the quoted draft ID and existing account-access checks.
Existing exact-ID API and advanced text commands remain supported. Send approval,
review expiry, account validation and no-resend behavior are unchanged.

Reviews show meaningful settings (channels, timing, schedules, routing, snooze and
resolution) and readable message context rather than implementation field names.
This installation uses Jacob and Elliot display overrides and Pacific time. These
are display preferences only; account IDs still determine all authority.

Tests: `telegram_presentation.test.js` covers generated-ID filtering, local times,
model labels, each alert/review family, literal source bodies, and exact hidden
status-button routing without any send. Existing confirmation/revocation/digest
and contact-task tests validate the same behaviors with the new display text.
