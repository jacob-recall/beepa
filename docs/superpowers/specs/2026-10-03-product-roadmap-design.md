# Beepa product roadmap — design specs

Date: 2026-10-03. Author: Claude, from a Q&A with the product owner. Status: DRAFT for review.

## 0. Decisions recorded from the Q&A

| Topic | Decision |
|---|---|
| Audience | Manager console and teammate app equally. Manager must see everything the teammate sees; the teammate app should be a faithful clone of the native messaging app. |
| Double text | The failure that bit us: teammate replied natively from their phone, the Beepa draft still showed pending, and they sent it too. |
| Suggestion flow | Ghost draft inside the conversation's composer. No separate inbox. A conversation holding a pending draft gets an unread-style badge in the chat list. |
| Manager side | Today only one suggestion bubble is visible per conversation (`apps/master/main.js` `showSuggestion` keeps one `.suggested` row by design). Manager wants to stack several and wants suggest-then-send to be one gesture. |
| CRM | Local to Beepa. No Notion code. The user picks tracked people, defines cadence classes (name + interval), assigns people to classes. People list sorted by most overdue. Notion stays manual. |
| Summaries | Deferred entirely. |
| Schedule send | Both teammate self-schedule and manager-timed proposals. Must fire with the browser closed. |
| Read state | (a) Teammate sees whether the other party read their message. (b) Unread state is correct across Beepa sessions. (c) Manager sees the same read state the teammate sees. |
| Lost/slow | Master lags the phone by minutes. Teammate's own phone-sent messages do not show as sent. "Just kind of fucky", needs thorough multi-angle testing. New contacts and new conversations also propagate poorly. |
| UI pain | Chat list gives no attention signals. People view is a flat list. Conversation view does not make clear who said what. Manager console is the worst. |
| Identity | Bridge ghost profile first, macOS Contacts photo/name as override when a contact profile links the room. |

## 1. Operating principle

Failure is more expensive than a missing feature. Every spec below carries a **fail-closed rule**: when the system cannot prove the state it needs, it shows less, sends nothing, and says so. No feature may add a second send path; every send still goes through `sendConvoMessage()` (browser) or the uplink's gated dispatcher (daemon).

## 2. Priority order and difficulty

Difficulty: **S** under a day, **M** 2–4 days, **L** 1–2 weeks, **XL** more. Risk: **security** means it touches a send path, consent, CSP, or the uplink's gates and gets a security review before and a verifier after.

| # | Feature | Diff | Risk | Why this slot |
|---|---|---|---|---|
| 0 | Propagation reliability baseline | L | medium | Everything after depends on knowing a message arrived. Measures before fixing. |
| 1 | Read state + chat-list attention signals | M | low | Pure read side. Unread badge is the substrate for drafts, overdue, and lost-message detection. |
| 2 | Ghost drafts, stale-draft retirement, multi-suggest | M | **security** | The double-text fix. Needs feature 1's freshness signals. |
| 3 | Names and avatars in the teammate app | M | low | No CSP change needed (`img-src blob:` already allowed). Big legibility win for small code. |
| 4 | CRM: tracked people, cadence classes, overdue list | M | low | Pure account-data + arithmetic over existing timeline data. |
| 5 | Shared read-only render layer + manager-console parity | L | medium | Makes 1–4 appear on the master without a second hand-rolled copy. |
| 6 | People view redesign | M | low | Builds on 3 and 4. Cosmetic once the data is there. |
| 7 | Schedule send | L | **security** | New durable queue in the daemon. Last because it is the one feature that can send on its own. |
| — | Summaries | — | — | Deferred. |

## 3. Feature specs

### F0. Propagation reliability baseline

**Goal.** Know, per message, where it is and how long each hop took. Find and fix the "sent from phone, shows as received or not at all" and "master lags by minutes" defects with evidence, not guesses.

**Mechanism.**
- Add a hop-timing record to every mirrored event: the uplink already stamps `com.jkali.origin_ts`. Add `com.jkali.hops` = `{bridge_ts, local_ts, uplink_seen_ts, master_ts}` on the mirrored copy only. Read-only data; nothing acts on it.
- A `tests/integration/probe_propagation.py` that sends a canary into a throwaway local room, measures time to local `/sync`, time to uplink ingestion (`state.db` event_map), time to master timeline. Fails if any hop exceeds a budget (default 15 s local, 60 s master). Runs against the real stacks on demand, not in `tests/run.sh`.
- Health panel in Settings (teammate) and per-teammate card (manager): last uplink pass age, pending delivery count, oldest pending age, last error. Reads the existing `publish_health` output (`uplink.py` ~line 2585).
- **Sent-not-shown audit.** For each source, confirm the mirrored event carries the from_me signal the renderer trusts. iMessage stamps `com.jkali.from_me` from the bot. For mautrix bridges the sender is the user's own mxid only when double-puppeting works; otherwise it is a ghost and renders as received. Spec: the uplink and renderer treat `ev.sender ∈ S.selfMxids ∪ {bridge double-puppet ids}` as sent, with the id set learned from the bridge's management room, never from message content. Produce a per-bridge table of what actually arrives before changing any render rule.
- **New conversation / new contact propagation.** Today new bridge rooms wait for `joinBridgeInvites()` (browser) and contacts mirror on `reconcile_ms`. Measure both. Likely fix: let the uplink also accept bridge invites through the same `invites.js` predicate ported to Python (parity test like consent), so a closed browser does not stall discovery. Flag as **security** because invite acceptance is identity-gated.

**Fail-closed rule.** The health panel says "unknown" when the uplink has not reported in 2 minutes. It never guesses "healthy".

**Tests.** Probe script with budgets. Unit test for the hop stamp. Integration scenario `12_phone_sent_renders_sent` per source that supports double-puppeting.

**Out of scope.** Fixing bridge-internal latency. If WhatsApp's bridge takes 20 s, we show 20 s.

### F1. Read state and chat-list attention signals

**Goal.** The chat list tells you what needs you. Unread survives reload. "Read" shows under your last message when the other party read it.

**Data.**
- Unread: Synapse's `unread_notifications.notification_count` from `/sync` per room, plus `m.fully_read` account-data per room written when a conversation is opened and the tail is at the bottom. No local counting.
- Other-party read: `m.receipt` ephemeral events in `/sync` for the room. mautrix bridges emit `m.read` receipts for remote users when the network provides them (WhatsApp, iMessage, Instagram do; LinkedIn and X partially). Render "Read" under the latest own message whose event_id ≤ the remote receipt's event. Render nothing when there is no receipt. Never infer.
- Mark-as-read on the phone: send `m.read` receipt to local Synapse when opening a conversation. Bridges that support it forward it to the network. This is what makes the native app agree. Settings toggle, default on.

**UI.**
- Row gets a left-edge indicator stack, max one dot per kind: unread (blue, with count), draft pending (purple, F2), overdue (amber, F4), reconnect (existing red).
- Sort: default by last activity. A "Needs attention" filter pins rows with any indicator to the top, keeping activity order within.
- Conversation view: "Delivered"/"Read" caption under the last own bubble. Only when a receipt exists.

**Mechanism.** `shared/ui/account-data.js` already consumes `/sync`; extend `buildConvos()` to carry `unread`, `fullyRead`, `remoteReadUpTo`. `openConvo()` posts the receipt and the fully_read marker once the tail has rendered. Keep the ephemeral receipt handling in `chat.js` tail loop.

**Fail-closed rule.** Receipt send failure logs and does nothing else. A missing receipt renders no caption. Unread count comes only from the server.

**Tests.** Unit: indicator stack from a convo record. Integration: open convo → `m.fully_read` written → reload → no unread.

### F2. Ghost drafts, stale-draft retirement, multi-suggest

**Goal.** Kill the double text. A manager suggestion appears as a greyed draft in the target conversation's composer. If anyone messages in that conversation after the suggestion was made, the draft retires itself and becomes a small "manager suggested at 14:02" note in the timeline. The teammate never sees a pending draft that the conversation has moved past.

**Teammate UI.**
- Remove the Proposals tab. Keep `proposals.js` parsing and classification; replace its render with two things: composer ghost (`prefillComposer` already exists) and a draft-pending indicator in the row (F1).
- Ghost composer: draft text in the composer at 50% opacity with a "Suggested by <manager> · 3 min ago" caption and two buttons, Send and Dismiss. Typing replaces the ghost; the ghost's text is kept behind an "Undo" for 10 s.
- Retirement: a draft is **pending** only when `origin_ts > lastActivityTs(room)` where lastActivityTs is the latest `m.room.message` from anyone, including bridge echoes of the teammate's phone sends. This is a pure function over data already in `feedModel`. When it flips, the composer clears and the timeline gets an inline grey note. The indicator in the row clears.
- Multiple pending drafts for one room: show the newest in the composer with a "2 more ↕" switcher. Sending one retires the others for that room (today's `pendingForRoom` picks newest and marks only that one; extend to mark all).
- Direct conversations: `com.jkali.auto_sent` records already render as "Sent directly". Keep.

**Manager UI.**
- Stacking: `showSuggestion` keeps one bubble. Change to one bubble per pending proposal, each with its retired/sent/pending state read from the mirror timeline (sent = `sentProposals`, retired = any later mirrored message). The overlay becomes a list, newest at the bottom, pinned under the last message.
- One gesture: the suggest bar sends on Enter and the bubble appears immediately in its optimistic "sending" state. Edit-in-place stays on double click. Remove the separate confirm for room-targeted suggestions; keep it for person-targeted ones (iMessage new chat), which can start a conversation.
- Show the teammate's read position (F1 data mirrored by F5) next to the suggestion so the manager knows whether it has even been seen.

**Uplink (Direct auto-send) — security.** Add gate D2-12 **conversation quiet since origin**: refuse when any `m.room.message` in the target room has `origin_server_ts > proposal.origin_ts`. This is the daemon-side twin of the retirement rule and is what prevents the auto-send variant of the double text. Reuse the existing refusal record path; reason string `"superseded"`. Python and JS must agree on the rule; add it to the conformance harness alongside consent.

**Fail-closed rule.** If `lastActivityTs` is unknown (room history not loaded), the draft is **not** shown as pending and the row shows no draft indicator. The daemon refuses when it cannot read the room's recent timeline.

**Tests.** Unit: `isPending(proposal, lastActivityTs)` table. Unit: multi-draft retirement on send. Integration: proposal → teammate phone reply arrives → draft retired, no send possible. Integration: Direct + later message → gate refuses with `superseded`.

### F3. Names and avatars in the teammate app

**Goal.** Every bubble in a group shows who. Every row shows a face.

**Data.** `m.room.member` `displayname` and `avatar_url` for the sender, from room state already fetched by `convoDisplayName()`. Avatar bytes via authenticated media GET → `blob:` URL (CSP already allows `img-src blob:`). Cache blobs per mxc in memory with a cap. Contacts override: when a contact profile links the room and the profile has a photo, use it. Photo lands in `contacts.db` as a new nullable `photo_blob` column written by `import_macos.py` (JXA `image` property, downscaled to 96 px, JPEG); exposed through the existing `/contacts/list` helper as base64. Mode 600 holds.

**UI.** Row avatar: image or initials fallback. Group bubble: 24 px avatar + name on the first bubble of a run from the same sender. DM: no per-bubble avatar. Platform chip stays. Sent bubbles never show an avatar.

**Fail-closed rule.** Any failure → initials. Never render a filename or a remote URL. mxc validated by `MXC_RE` before fetch.

**Tests.** Unit: name/avatar resolution precedence (contact photo > ghost avatar > initials). Unit: `MXC_RE` rejection path.

### F4. CRM: tracked people, cadence classes, overdue list

**Goal.** "I haven't talked to X in 3 weeks and I meant to every 2." Local only. No Notion code.

**Data.** New account-data `com.jkali.crm`:
```
{ classes: [{ id, name, every_days }],
  tracked: { <profileId>: { class_id, note } } }
```
Normalized on every read like `contact_profiles`. Cap 50 classes, 2000 tracked. A tracked entry references a contact profile from `shared/model/contacts.js`; an unlinked profile id is dropped on read.

**Computation.** `lastTalked(profile)` = max `lastTs` across the profile's `roomIds`, split into `lastFromMe` and `lastFromThem` using the F0 sent/received resolution. Overdue days = `now − max(lastFromMe, lastFromThem) − every_days`. Default sort by overdue descending. The user can toggle "count only my messages" per class.

**UI.** People tab gets a "Tracked" section at the top: avatar, name, class pill, "last talked 19d ago · 5d overdue", tap opens the most recent conversation. Settings → Cadence classes: add/rename/delete, interval in days. Per-person: a class picker on the contact page. Overdue indicator on chat rows (F1).

**Manager.** Mirror `com.jkali.crm` to master only for tracked profiles that have at least one room currently at `share` or `direct`. A profile with only private rooms is never named on the master. Reuse the contacts mirror diff path (`mirror_contacts`). Manager console shows the same Tracked list per teammate (F5).

**Fail-closed rule.** CRM writes are merge-over-fresh-read like every consent write; a failed read disables the controls. The mirror skips any profile whose share state cannot be point-read.

**Tests.** Unit: normalize + overdue arithmetic. Unit: mirror eligibility (private-only profile never mirrored). Conformance: eligibility rule in both JS and Python.

### F5. Shared read-only render layer and manager-console parity

**Goal.** The manager sees exactly what the teammate sees: avatars, names, unread/read state, drafts, overdue. Without copying code by hand a third time.

**Mechanism.** Split `shared/ui/` into `shared/ui/read/` (render.js bubble + row builders, identity resolution, indicators, timestamps; zero imports of chat.js/sources.js) and the existing send-bearing modules. The master imports only `shared/ui/read/`. A build-time check (`tests/unit/master_no_send.test.js`) asserts by static import graph that nothing under `shared/ui/read/` reaches `sendConvoMessage` or `sendCmd`. This preserves the "absent code" property with a test instead of duplication.

**Uplink additions.** Mirror per room: sender display/avatar (upload avatar mxc to master through the existing media path; stamp `com.jkali.sender_display`, `com.jkali.sender_avatar`), the teammate's `m.fully_read` position and remote `m.read` receipts as a `com.jkali.read_state` state event on the mirror (receipts cannot be forged across homeservers, so a state event the uplink owns is the honest carrier). CRM eligible entries (F4).

**Fail-closed rule.** Master renders the same initials/no-caption fallbacks. Read state older than the mirror's last event shows as "unknown".

**Tests.** Static import-graph test. Integration: read state round trip to master.

### F6. People view redesign

**Goal.** Search, one card per person, faces, platform chips, merge suggestions inline. Not a flat list.

**UI.** Search box filtering by name and handle. Sections: Tracked (F4), Recent, All (alphabetical, letter index). Card: avatar (F3), name, chips per linked platform, last talked. Tap → contact page with linked conversations, class picker, share controls as today. Merge suggestions (`suggestions()` is advisory already) render as a dismissible banner on the card, "Also +1 415… on WhatsApp? Link". Linking stays a click; nothing auto-links beyond the existing `autoMergeByNumber`.

**Fail-closed rule.** Unchanged consent semantics. Linking never shares.

**Tests.** Unit: search filter and section bucketing.

### F7. Schedule send — security

**Goal.** Teammate picks "send at 9:00 tomorrow" on their own draft; manager picks a time on a suggestion. Fires with the laptop lid open and the browser closed. Never fires twice. Never fires into a conversation that moved on without saying so.

**Mechanism.**
- Teammate: the browser writes a `com.jkali.scheduled_send` event `{ target_room, body, send_at, created_ts }` into the teammate's existing local proposals room (already uplink-owned, already polled). Cancel = a `com.jkali.scheduled_cancel` referencing the event id, or editing the draft (which cancels and re-creates).
- Daemon: the uplink's proposal stage gains a `scheduled` queue in `state.db` with `(event_id, target_room, body_hash, send_at, state, outcome)`. At `send_at`, dispatch through the **same** D2 gate function used for Direct auto-send, with these differences: the sender-verification gate accepts the teammate's own mxid instead of the manager; the consent gate is skipped because this is the teammate's own message; freshness compares to `send_at`, not `created_ts`. All other gates apply, including the new D2-12 quiet-since gate: if the conversation had activity after `created_ts`, the send is **held**, not dropped, and surfaced in the teammate app as "Scheduled message held: conversation moved on. Send now / Cancel". Rate cap applies.
- Manager-timed proposal: `send_at` on the proposal. For `share` rooms it becomes a ghost draft with the time shown; the teammate confirms, and from then on it is their scheduled send. For `direct` rooms the Direct dispatcher honors `send_at` with the same gates.
- Outcomes use the existing accepted/refused/ambiguous ledger. The teammate app renders "Scheduled for 9:00" in the composer area and a clock on the row.

**Why the uplink and not a new daemon.** It already runs, already has the durable outcome ledger, rate cap, and ambiguity handling, and is already the one sanctioned daemon send path. A second daemon would be a second path.

**Fail-closed rule.** Clock skew > 5 min between browser and daemon → refuse to schedule with a visible message. Daemon down past `send_at` → held with a notice, never a late silent fire. Any gate failure → held or refused, visible, never silent.

**Tests.** Unit: scheduler state machine. Conformance: gate list parity. Integration: schedule → daemon restart → fires once. Integration: activity after schedule → held.

## 4. What is not being built

- Notion integration. The CRM is local; Notion is maintained by hand.
- Summaries or any LLM call.
- Any new send path outside `sendConvoMessage()` and the uplink's gated dispatcher.

## 5. Suggested first slice

F0's probe script and health panel, then F1. Both are read-only, both make every later feature testable, and F1's indicator stack is where F2's draft badge and F4's overdue badge land.
