# Plan: iMessage Phase 2 — richness + guided permission wizard (imsg-p2-v1)

Builds on the verified Phase 1 connector (PLAN-IMSG.md; M-1..M-22 GOVERNING
and still apply). Same daemon (imessage/daemon.py), same launchd service,
same security posture. Two independent workstreams:

## Workstream A — message richness (daemon, both directions)
Additive parsing/relay in the existing poll (inbound) and transaction handler
(outbound). No new trust boundary; all Phase 1 controls (sender allowlist,
mapped-chats-only, rate cap, idempotency, hash ledger, body-free logs) apply
unchanged to the new event types.

- **A1 Reactions/tapbacks**: inbound engine reactions → Matrix `m.reaction`
  (m.annotation rel) from the ghost; outbound Matrix `m.reaction` from @jkali
  → engine `react`/`unreact`. Engine reaction schema (heart/like/laugh/etc.)
  ↔ emoji map, finalized against real `messages --json` output in-slice.
  Ledger extended to hash reaction (target+key) for echo suppression.
- **A2 Edits**: inbound edited message → Matrix `m.replace` on the original
  event (requires event-id memory: extend `seen_msg` to store the mapped
  Matrix event_id per iMessage message id); outbound `m.replace` from @jkali
  → engine `edit`.
- **A3 Backfill on new portal**: when a portal is first created, pull the last
  N messages (config `backfill_count`, default 25) via `messages` and post
  them oldest-first as the ghost/bot before marking live. Bounded, idempotent
  via seen_msg.
- **A4 (lower priority, include only if ephemeral events are enabled)**:
  read receipts + typing. Requires adding `de.sorunome.msc2409.push_ephemeral`
  to the registration and enabling ephemeral in the appservice. Deferred
  unless trivial — flagged as optional in acceptance, not a blocker.

## Workstream B — guided permission wizard (in the hub)
Goal: connecting iMessage becomes click-through in the hub, like WhatsApp's
QR — no manual System Settings hunting.

Mechanism (the hub is static JS talking only to Matrix, so all control flows
through the bot): add an iMessage **management room** (bot + @jkali only,
created by the bot, mirrors WhatsApp's mgmt room) and a command handler in the
daemon for messages in THAT room from @jkali only (reuses M-1 allowlist).

- **B1 Bot commands** (daemon-side, management room only):
  - `status` — the daemon probes each grant with a benign read and replies
    with a plain-text checklist (✓/✗ per: Messages Data via `current-user`
    non-error; Contacts via presence of any contact name in `chats`;
    Accessibility/Automation via a dry-run capability check — probed by the
    known CLI error-string on a no-op, NEVER by sending a message). Honest
    about which grants can and cannot be probed without side effects.
  - `setup` — replies with the checklist AND, for each missing grant, the
    daemon runs `open "x-apple.systempreferences:com.apple.preference.security?Privacy_<Pane>"`
    to deep-link the exact pane. `open` is invoked list-argv, shell=False,
    with a hardcoded allowlist of the 3-4 exact URL strings (no interpolation
    of any Matrix-derived data — the command word only selects from the
    fixed set).
  - `help` — lists these.
- **B2 Hub iMessage card**: the Connections view gains an iMessage card
  alongside WhatsApp. It shows connection status (parsed from the bot's
  `status` reply), a **Set up iMessage** button (sends `setup`), per-missing-
  grant **Open Settings** affordance (re-sends `setup`), and auto-refreshes
  the checklist by re-issuing `status` on a timer while the card is open.
  Reuses the existing hub command/console plumbing and all hub security
  invariants (textContent-only, mgmt-room verification before sends, etc.).

## Security constraints (additions; Phase 1 M-* unchanged)
- **N-1** New command surface (`status`/`setup`/`help`) executes ONLY for
  messages whose sender is exactly @jkali:localhost in the iMessage
  MANAGEMENT room (verified: bot+jkali, exactly 2 members, not a portal —
  same check as M-6/H-6). Portal-room messages remain relay-only, never
  parsed as commands.
- **N-2** `open` deep-links use a fixed allowlist of exact URL constants; the
  command never passes Matrix-derived text to `open` or any shell; shell=False
  list-argv. The only thing a command controls is which fixed URL (or none).
- **N-3** Probes are READ-ONLY and side-effect-free — never send an iMessage,
  never create/delete a chat, never touch a real contact. Accessibility can
  only be probed indirectly; if unprobeable without a send, report it as
  "unknown — grant it if sending fails" rather than sending a probe.
- **N-4** Reaction/edit/backfill content obeys the same clamps as M-9
  (sanitize, length cap, textContent in hub); backfilled bodies are hashed in
  the ledger only if needed for echo suppression, never stored plaintext
  (M-13). Event-id map rows contain ids only, no bodies.
- **N-5** If A4 (ephemeral) is built, the registration change is backed up +
  reversible and re-verified not to break the WhatsApp appservice; read
  receipts/typing carry no message bodies.

## Slices
- **P2.0 prerequisite (in P2.1)** — establish the `(chat_id,msg_id)->event_id`
  map (same PK shape as seen_msg), written whenever the daemon posts a
  message event; R-1 target resolution (both slices) depends on it, so it
  lands before P2.1's cross-room check runs.
- **P2.1** — A1 reactions both directions. *Acceptance (self-chat)*: a
  tapback added in Messages appears as m.reaction in Matrix within 60s; a
  Matrix m.reaction from @jkali produces exactly one engine react (idempotent,
  no echo loop).
- **P2.2** — A2 edits both directions. *Acceptance*: editing an iMessage
  edits the Matrix event (m.replace on the same event_id); a Matrix edit from
  @jkali produces one engine edit, no echo.
- **P2.3** — A3 backfill. *Acceptance*: deleting the self-chat portal row +
  restarting → the recreated portal is prepopulated with the last N messages
  oldest-first, each exactly once (no dupes on re-poll).
- **P2.4** — B1 bot commands. *Acceptance*: `status` in the mgmt room returns
  a checklist reply; `setup` returns the checklist and (observed) opens a
  System Settings pane; a `status`/`setup` sent by a GHOST or in a PORTAL room
  produces zero command execution (N-1); ALSO a status/setup sent from a
  non-portal room whose only two members are bot+@jkali but which is NOT the
  persisted `mgmt_room` produces zero command dispatch and zero `open`
  (proves B-2 is by persisted id, not member count); `open` argv is a fixed
  allowlisted URL (code inspection + a probe that a crafted command body
  cannot alter the opened URL).
- **P2.5** — B2 hub card. *Acceptance*: the hub Connections view shows an
  iMessage card; its Set-up button drives the bot `setup`; the checklist
  renders and refreshes; no new HTML-string sinks; hub CSP unchanged.
- **P2.6** — Verification: fresh pilotfish:verifier on the exact claim
  (reactions+edits both ways with echo suppression + idempotency; backfill
  populates once; command surface gated to the PERSISTED mgmt-room + @jkali
  incl. the non-persisted-2-member negative case (B-2); B-1 command limiter
  caps AND >120s-freshness drop of forged fresh-txn-id `setup`; R-1 cross-room
  edit/reaction target drop; `open` allowlist un-influenceable by message
  content; WhatsApp + Phase 1 text loop still work; no secret-mode
  regressions; listeners unchanged).

## Rollback
Per-slice: revert the daemon change and restart the launchd agent; the mgmt
room/commands are inert if the handler is removed. B2: revert hub/site files.
A4 registration change (if built): restore homeserver.yaml backup, restart
Synapse, re-check WhatsApp + iMessage bots. No data loss (additive).

## Stops
Any slice failing after 2 distinct fix attempts → stop and report. Engine
tests use ONLY the self-chat (M-15 caps). Never automate permission dialogs.
Never send a probe that emits a real iMessage.

## Security review dispositions (2026-08-26) — GOVERNING over draft text
A4 ephemeral: DROPPED (A-1: only registration-touching item, EDU firehose
into a synchronous handler, cosmetic benefit).
- **B-1 (P1)** Command limiter: ≤1 `open`/10s, ≤5/min, ≤20/hour; identical
  URL dedup 60s; ONE pane per `setup` invocation (the first missing grant);
  freshness gate — command events with origin_server_ts older than 120s are
  dropped (defeats fresh-txn-id replays of captured transactions).
- **B-2 (P1)** Mgmt room pinned: daemon stores mgmt room id in `meta` at
  creation, stamps marker state `com.jkali.bridge.mgmt`/`imessage`, never
  sets uk.half-shot.bridge on it; command dispatch requires
  `room_id == meta_get('mgmt_room')` AND `chat_for_room(room_id) is None`
  (portal branch wins). Hub selects the iMessage mgmt room ONLY by the
  marker state and never auto-creates one for iMessage.
- **
## Security review dispositions (2026-08-26) — GOVERNING over draft above
Verdict: sound in shape; fold in B-1..B-9, R-1..R-6; DROP A4. Where this
section conflicts with the draft, THIS governs.

Command surface:
- **B-1 (P1)** Command limiter separate from rate_ok (which is chat-keyed):
  `open` ≤1/10s, ≤5/min, ≤20/hr; dedup identical URL within 60s; `setup`
  opens ONE pane per invocation; freshness gate — drop command events whose
  `origin_server_ts` is >120s old (defeats fresh-txn-id replay of a captured
  transaction).
- **B-2 (P1)** Mgmt room identified by PERSISTED id + marker, never by member
  count. Daemon: create mgmt room, store id in meta `mgmt_room`, stamp a
  `com.jkali.bridge.mgmt` state event (state_key `imessage`), NEVER set
  `uk.half-shot.bridge` on it. A room is a portal XOR the mgmt room, decided
  by the map with the portal branch first: command dispatch requires
  `room_id == meta_get('mgmt_room') and chat_for_room(room_id) is None`. Hub
  selects the iMessage mgmt room by the marker + absence of half-shot bridge,
  and NEVER auto-creates one for iMessage.
- **B-3 (P3)** Command dispatch exact-match, relation-free: require
  `msgtype=='m.text'`, no `m.relates_to`, `body.strip().lower() in
  {status,setup,help}`. No prefix/split dispatch.
- **B-4 (P3)** Mgmt room: bot-creates-and-invites (mirrors M-22); no auto-join
  of arbitrary rooms; any join gated to sender==@jkali + mgmt room only.
- **B-5 (P3)** Probes read-only: `current-user`/`chats` only; anything not
  probeable without a side effect reports "unknown", never a test send.
- **B-6 (P4)** `status` replies OK/not-OK per grant; never echoes the Apple
  ID into the archive/console.
- **B-7 (P3)** `open` allowlist: absolute `/usr/bin/open`, dict lookup with
  NO default (missing key = no invocation), four exact URLs —
  `Privacy_Accessibility`, `Privacy_Contacts`, `Privacy_Automation`,
  `Privacy_AllFiles` (Full Disk Access = the "Messages Data" grant; there is
  no Messages-Data pane). Deep-link failure is non-fatal, non-retried; reply
  always includes the manual path text.
- **B-8 (P4) accept** (LaunchServices scheme-hijack = local-malware, trusted
  set). **B-9 accept**: no extra confirm (idempotent, user-initiated, and a
  txn-forger bypasses hub dialogs anyway); B-1's bound is the control.

Reactions/edits/backfill:
- **R-1 (P1)** Event-id memory keyed `(chat_id,msg_id)->event_id` (matches
  seen_msg PK). Inbound: assert resolved event's room == room_for_chat(chat)
  before posting m.replace/m.reaction. Outbound: resolve target event ->
  (chat,msg); assert chat_for_room(ev.room_id)==chat before any engine
  edit/react, else drop. No cross-portal targets.
- **R-2 (P2)** Reaction keys are arbitrary remote text: clean_text then cap
  ≤8 codepoints, reject empty, map schema values via lookup WITH default,
  never interpolate the raw engine value.
- **R-3 (P2)** react/unreact/edit consume the per-chat rate limiter too.
- **R-4 (P2)** Backfill: `meta` flag `backfill:<chat>:<room>` written BEFORE
  posting; backfilled ids inserted into seen_msg; caps: backfill_count ≤50,
  per-daemon-start global ≤500 posts, portal-creation rate cap. P2.3
  acceptance reworded: NOT "delete row+restart repopulates" (that contradicts
  seen_msg) — instead: a fresh chat's first portal backfills once; a
  re-poll/restart adds zero dupes.
- **R-5 (P4)** Ledger keyed on `sha(chat_id+\0+payload)`; reactions hashed as
  (chat,target_msg,key). Fixes global cross-chat suppression.
- **R-6 (P3)** Outbound redaction handled ONLY for reaction events the daemon
  mapped, same room; a message-event redaction never reaches an engine call
  (no unsend/delete — consistent with the never-delete-chat stop).
- **A4/A-1**: DROPPED from Phase 2 (only item touching registration/
  homeserver.yaml; EDU firehose on the synchronous handler; cosmetic). Not
  built.

C-1 (shared with Phase 3): hub `sendCmd` mgmt-room verification becomes
UNCONDITIONAL for every send (not just destructive) — status/setup are
exactly the sends that must never land in a portal.

Revised P2.4 acceptance adds: 50 forged fresh-txn-id `setup` events →
≤ cap open invocations; a >120s-old `setup` → zero; `setup` from a ghost or
from a portal → zero command exec (portal case relayed-only); `open` argv is
an allowlisted constant uninfluenced by body. P2.1/P2.2 add the R-1 cross-
room-target drop and R-3 limiter checks.

## Model routing (cyber flagging, 2026-08-26)
Per policy, security-sensitive implementation is NOT written by Fable (main)
or a general executor — it routes to pilotfish:security-executor (Opus) after
approval; pre-approval analysis already went to pilotfish:security-reviewer
(Opus). Main/Fable owns framing, integration, and non-security UI only.

SECURITY-FLAGGED → pilotfish:security-executor (Opus):
- **P2.4 (B1 command surface + permission wizard)** — new host-action sink
  (`open`), command dispatch gating, mgmt-room authz. Carries B-1 (command
  rate/freshness limiter), B-2 (mgmt-room pinning + marker), B-3 (exact-match
  dispatch), B-5 (read-only probes), B-6, B-7 (`open` allowlist / argument-
  injection). Authz + hardening + injection surface.
- **R-1 across P2.1/P2.2** — cross-room edit/reaction target scoping (authz).
- **R-2, R-3, R-5, R-6** — remote-content sanitization, rate-limiter coverage,
  ledger keying, redaction restriction (input validation / hardening).
- **R-4 backfill bounds** — amplification/DoS caps (hardening).
- **C-1** — unconditional hub mgmt-room verification (authz).

NON-SECURITY → main (Fable) or pilotfish:executor (Sonnet), but any rendering
of bridged/remote content still follows the sanitization contract and is
verified:
- **P2.5 (B2 hub card)** layout/plumbing — reuses existing hub security
  invariants; the invariants themselves are not re-implemented here.
- **A1/A2/A3 relay mechanics** (emoji-map display, m.replace/m.reaction event
  construction, backfill posting loop) — the *validation* parts of these are
  the R-* items above and route to security-executor; the plain relay wiring
  does not.

Every security-flagged slice also gets a fresh pilotfish:verifier (Opus) pass
(P2.6), unchanged.

### P2 execution outcome (2026-08-26)
Daemon (security-flagged) built by pilotfish:security-executor (Opus): reactions,
edits, backfill, event_map, mgmt room + status/setup/help commands, all M/N/R/B
controls; compiles + Opus self-tested sanitizers/limiters/freshness. Restarted +
verified by Fable (main): mgmt room created; help/status replies correct; setup
opened exactly ONE pane (Contacts) then rate-capped the immediate repeat; ALL
negative controls zero-`open`/zero-engine (B-2 non-mgmt room, portal, ghost
sender, >120s stale, m.relates_to; R-1 cross-portal reaction target). Reaction/
edit inbound loop depends on the chat timestamp bumping on tapback (executor
caveat) — not fully exercised (self-chat had none), deferred to live use. Hub
iMessage card (P2.5) folded into the Phase 3 app.js build (shared file, shared
security logic) rather than built twice.
