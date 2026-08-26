# Plan: native in-hub conversation view (replace Element pane for read/reply) — convo-v1

User chose (2026-08-26) to build the hub's OWN conversation screen so opening a
chat from Home stays in one consistent, filtered UI instead of dropping into
Element. Extends the Hub (PLAN-HUB/IMSG3/HOME, GOVERNING). Static hub, Matrix
client API at 127.0.0.1:8008, @jkali token in sessionStorage. Security-flagged
(renders bridged message content in the hub DOM + adds a message-send surface)
→ pilotfish:security-executor (Opus) builds app.js; Fable verifies; layout/CSS
by Fable.

## Outcome (v1, text-first)
- Clicking a Home (or per-source list) conversation opens a NATIVE hub view
  `#view-convo` — NOT the Element iframe. It shows: a header (back-to-Home,
  chat name + platform badge, an "Open in Element" escape-hatch link), a
  scrollable message list (recent history), and a compose box to type + send.
- Read: recent messages rendered in the hub — sender name, text body,
  timestamp, sent-vs-received styling. Media/attachments show a neutral label
  ("Photo"/"Video"/"File") in v1 (no inline media yet). Reactions/edits: v1
  may ignore or show minimally.
- Reply: type in the compose box → sends a normal Matrix `m.room.message` to
  that room; the bridge routes it to the right network. Identical for every
  platform.
- Live: new messages in the OPEN room append in real time; sending appends
  your message (optimistic or on echo).
- The Element iframe stays available ONLY via the explicit "Open in Element"
  link (full-feature fallback); Home no longer auto-opens Element.
- Consistency: because it's the hub's own DOM, Home's hidden/muted filtering
  and the unified look carry through — no separate Element list.

## Security constraints
- **CV-1 (render)**: message bodies rendered ONLY via sanitize→textContent;
  ONLY `m.room.message` with string `content.body` and msgtype ∈
  {m.text, m.notice}; media msgtypes → a STATIC label derived from msgtype
  (never the bridged filename); NEVER read `formatted_body`/HTML; sender
  display name via sanitizeLine. No innerHTML-family sinks (Trusted Types).
  Sanitize strips C0/C1/bidi/zero-width; bodies clamped to a sane max; the
  message list is bounded (cap rendered messages, e.g. ≤200; paginate older on
  scroll-up, bounded).
- **CV-2 (send)**: compose sends `PUT /_matrix/client/v3/rooms/{roomId}/send/
  m.room.message/{txn}` with `{msgtype:'m.text', body:<user text>}` via api().
  `roomId` is ONLY the currently-open room, which must be a validated member
  of feedModel/joinedSet AND pass ROOMID_RE — never a typed/bridged value.
  txn id is a fresh random. body is the user's own typed text (clamp length).
  This is a new send surface but within the existing token scope (the user can
  already send via Element) — no capability increase; the control is that the
  destination is the validated open room only.
- **CV-3 (isolation, D-3)**: the conversation read/send path is SEPARATE from
  the command/console path — it only reads/sends the open PORTAL room, never a
  mgmt room, never the command handler. Its live read shares no symbol with
  handleMgmtEvent/logConsole/reactToBotReply/sendCmd.
- **CV-4 (live/bounded)**: live updates for the open room via a scoped read
  (a room-scoped /sync or /messages poll); bounded memory (only the open
  room's recent window held); coalesce renders; long-poll timeout + backoff;
  stop the room watch when the view closes/another room opens.
- **CV-5 (no CSP change)**: read/send over connect-src 8008; NO new img host —
  v1 shows text + labels (no inline media). If a later version shows images,
  authenticated-fetch→Blob only (img-src 'self' blob:).
- **CV-6 (nav)**: opening a room still validates roomId (ROOMID_RE ∩ joinedSet)
  exactly like openConversation does today; the "Open in Element" link uses
  the same validated constant-prefix URL (U-1) into the iframe.

## Slices
- **CV.1 (layout, Fable)** — add `#view-convo` (header: #convo-back,
  #convo-title, #convo-badge, #convo-element-link; #convo-messages scroll
  list; #convo-compose input + #convo-send) + CSS (message bubbles sent/recv,
  header, compose bar). Nav: Home/list row click routes here.
- **CV.2 (app.js, Opus)** — implement: openConvo(roomId) validates + opens
  #view-convo, loads recent messages (GET /rooms/{id}/messages?dir=b or from a
  scoped sync), renders per CV-1; a room-scoped live watch (CV-3/CV-4) appends
  new messages; compose → send per CV-2; back button → Home; "Open in Element"
  → the existing validated iframe nav (escape hatch). Home/per-source row
  clicks call openConvo instead of the Element-navigating openConversation
  (keep openConversation for the Element escape-hatch only). Bounded, coalesced.
  *Acceptance*: opening a chat shows its recent messages in the hub (not
  Element); a crafted bridged message body (bidi/newlines/HTML/matching a
  status-parser regex) renders as sanitized single-safe text and never reaches
  the console/status parser; media msg → a static label, no bridged filename,
  no remote <img>; typing + send delivers an m.room.message to the OPEN room
  only (validated) and appears; a new incoming message appears live; back
  returns to the filtered Home; "Open in Element" still works; node --check
  passes; 0 HTML-string sinks; hub CSP byte-identical.
- **CV.3 (verify, Fable)** — confirm CV.2 acceptance at code + data level:
  send to a self-chat delivers (M-15 self only), render sanitization, isolation
  (no command-path symbols in the convo functions), headers unchanged, sinks
  clean; live-append via a self-directed test message.

## Rollback
Revert hub/site/{index.html,app.js,style.css}; restart hub. Home row clicks
revert to opening Element. No daemon/registration/homeserver change.

## Stops
Any slice failing after 2 fix attempts → stop and report. Engine-visible sends
target the user's own self-chat only during testing (M-15). No inline remote
media in v1 (CSP). Conversation view never sends to a non-open/unvalidated room.

## Security review dispositions (2026-08-26) — GOVERNING
Approved. Build CV.2 per these (all map to existing app.js primitives):
- **CV-R4 (P1)**: ONE shared `renderMessageEvent(ev)` renderer for BOTH history
  (/messages) and live-append, applying the exact feedPreviewFromEvent policy
  (m.text/m.notice body only; edits → m.new_content.body only; media → static
  msgtype label, never the bridged filename; NEVER formatted_body).
- **CV-I1 (P1)**: a THIRD independent room-scoped loop (own convoRunning/
  convoSince), server-filtered to `{room:{rooms:[openRoomId],...}}` + a
  client guard dropping events whose room != openRoomId; references NONE of
  routeMgmtEvent/handleMgmtEvent/logConsole/reactToBotReply/sendCmd/
  updateImsgCard; appends only to #convo-messages; stops (convoRunning=false,
  clear openRoomId) on close/room-switch.
- **CV-S1 (P1)**: compose re-validates at send: `ROOMID_RE.test(roomId) &&
  feedModel.has(roomId) && joinedSet.has(roomId)` AND roomId !== any
  runtime.*.mgmtRoomId; fresh txn(); clamp body length. No stale room trust.
- **CV-R1 (P2)**: sent-vs-received + ownership from `ev.sender === userId`
  (mxid) ONLY, never the display name; display name decorative via sanitizeLine.
- **CV-R2 (P2)**: sender / body / time are THREE separate el() nodes; body via
  sanitize (bounded), never concatenated into a chrome node.
- **CV-R3 (P2)**: #convo-messages holds ONLY bridged bubbles; hub errors/status
  go to a separate, distinctly-styled region (anti-phishing).
- **CV-D1 (P2)**: cap rendered bubbles ≤200 (drop oldest); /messages page ~50,
  older pages on debounced scroll-up with a retained-window cap; live long-poll
  timeout 25000 + 3s backoff; release window on close; dedup optimistic echo by
  event_id/txn_id.
- **CV-M1 (P3)**: media = static msgtype label (no inline remote img) → CSP
  byte-identical; future images only via authenticated-fetch→Blob (showQR
  pattern) + revoke.
- **CV-E1 (P3)**: "Open in Element" reuses the exact openConversation path
  (validated constant-prefix URL) on the open room id; embeds no bridged text.
- **CV-P1 (P3)**: reactions/edits IGNORED in v1; token/session/frame posture
  unchanged.

### Execution outcome (2026-08-26)
CV.1 layout (view-convo, bubbles, compose, convo-status CSS) by Fable; CV.2
logic (openConvo, renderMessageEvent single whitelist renderer, startConvoWatch
3rd isolated loop, sendConvoMessage, stopConvoWatch, nav rewire) by Opus.
Verified by Fable: node --check passes; 0 HTML sinks; NO command/console
symbols in any convo function (CV-I1 isolation); feed/list rows now call
openConvo (native) with openConversation kept only for the "Open in Element"
escape hatch; hub CSP byte-identical. Live: /messages history renders 37
whitelisted bubbles; compose send via the exact PUT delivers and is attributed
sent (ev.sender==userId → right bubble). This ALSO resolves the earlier
Element "browser not supported" gate — reading/replying no longer routes
through Element (native view instead; Element is escape-hatch only).
Reactions/edits/inline-media are v1-deferred (labels for media).

### Two-pane messenger layout (2026-08-26)
Home is now a native messenger: left pane = conversation list + search
(#msgr-list: #home-search, #home-list), right pane = the open chat
(#msgr-convo: #convo-head, #convo-messages, #convo-compose). #view-convo was
merged into #view-home; all element IDs kept so the convo logic + security
controls are untouched. Fable did index.html + CSS; Opus rewired app.js
(presentation/nav only): openConvo reveals the right pane without leaving the
list + marks the active row (setActiveConvoRow); navTo('home') preserves an
open chat or shows the placeholder; #convo-back deselects (mobile); no
security control changed. Verified: node --check, 0 sinks, no view-convo refs,
all DOM ids resolve, CSP byte-identical. Responsive: single-pane <720px.

### Unify "from me" attribution (2026-08-26)
Own messages render right-aligned as "You" regardless of app/origin.
- iMessage daemon (Opus): from_me relays (deliver_inbound/_relay_message AND
  reconcile_edit) drop the "[you] " prefix and set content
  `com.jkali.from_me: true` (sender stays @imessagebot — M-22 intact, no
  @jkali token). Received/ghost messages carry no flag.
- Hub UI (Opus): renderMessageEvent sent = ev.sender===userId OR
  (content['com.jkali.from_me']===true AND ev.sender===@imessagebot:localhost).
  ANTI-SPOOF: the flag is honored ONLY from our own bot — a ghost/remote
  sender carrying it is ignored (left-aligned). Unit-tested: 6/6 incl. 2 spoof
  cases rejected. node --check + py_compile + 0 sinks.
- KNOWN GAP (needs user decision): WhatsApp/gmessages messages the user sent
  from their PHONE come from the user's OWN ghost (@whatsapp_lid-...), with no
  from-me flag mautrix exposes — so they still render left. The correct fix is
  DOUBLE PUPPETING (bridge relays own messages as @jkali), which reverses the
  H-2/A-1 no-double-puppet decision (bridge gets a @jkali token). Offered to
  the user as a separate security-reviewed step. Existing pre-change iMessage
  messages keep the old [you]/left look; only new ones get the marker.

### Generalizable "who am I" self-identity (2026-08-26)
Right-aligns the user's OWN messages from any bridge WITHOUT double-puppeting
(no bridge gets a @jkali token; H-2/A-1/M-22 intact). Two derivation sources
unioned into selfMxids (hub, Opus-built):
1. Account data `com.jkali.self_identities` {mxids:[...]} — user-written (only
   the user's token can set it → spoof-safe authoritative override). Populated
   for this user with @whatsapp_lid-130249278910545:localhost.
2. Heuristic (GENERALIZABLE onboarding, zero setup): per source, the sender
   appearing in the MOST distinct chats = the user (you're in all your own
   chats); gated by ≥5 rooms AND ≥2× runner-up. Verified: user's WA ghost 14
   distinct rooms vs 2 → auto-identified.
renderMessageEvent: sent = ev.sender===userId OR (trusted iMessage from_me flag
from @imessagebot) OR selfMxids.has(ev.sender). selfMxids affects ALIGNMENT
ONLY — no send/validation/capability; iMessage anti-spoof gate unchanged.
Verified: node --check, 0 sinks, anti-spoof intact. Onboarding extension
point: the account_data is populated per-login (a resolver/login hook writes
each new account's own ghost); the heuristic covers users with none set.
Note: pre-existing WhatsApp messages already in Matrix keep their stored
sender (own ghost) → now right-aligned; nothing to backfill.
