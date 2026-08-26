# Plan: Phase 3 — Hub left-bar per-source tabs + cross-source Directory (hub-p3-v1)

Extends the Hub shell (PLAN-HUB.md, A1/A2 GOVERNING; hub is the single UI at
127.0.0.1:8010 framing Element at :8009). All hub security invariants stay:
Trusted Types, textContent-only, sessionStorage token, mgmt-room verification
before destructive sends, canonical 127.0.0.1 origins, frame chain unchanged.
This is UI + Matrix-client-API reads only — NO new backend, NO new ports, NO
new secrets, NO CSP change (Element already framable by the hub; the hub
already talks to :8008).

## Outcome (user's exact ask)
- A **left sidebar** in the hub. Top: source tabs — **All**, **WhatsApp**,
  **iMessage** (config-driven from a SOURCES array: each names its space +
  ghost prefix, so a new bridge = one array entry). A per-source tab shows
  that source's conversation list (rooms in its Matrix space); clicking a
  conversation opens it in the embedded Element pane
  (`:8009/#/room/<room_id>`).
- A **Directory** tab (also in the left bar): one search box across ALL
  sources. It searches the user's existing conversations in every space
  (local room-name/member filter, client-side). Per-source "start a chat"
  from a user-typed handle is capability-gated (D-1): WhatsApp
  `canStartChat:true` (existing gated `start-chat` path); iMessage
  `canStartChat:false` — disabled "not available yet", issues NO iMessage bot
  command (M-2). No iMessage remote search/resolve is used. Results grouped
  by source with a badge; manage actions reachable from one place.
- The old top-tab bar (Chats/Connections/Settings) folds into the sidebar:
  sidebar sections = Sources (All/WhatsApp/iMessage), Directory, then
  Connections and Settings at the bottom. "Chats/All" is the default.

## How the pieces work (all via the Matrix client API the hub already uses)
- **Spaces → conversation lists**: read each source's space via
  `/rooms/{space}/hierarchy` (or m.space.child state) → child room ids;
  fetch each room's name/avatar/last-message via `/sync` or per-room state.
  WhatsApp space + iMessage space ids are discovered by name or recorded.
- **Open a conversation**: the embedded Element iframe is navigated by setting
  its `src` to `http://127.0.0.1:8009/#/room/<room_id>` (Element route). The
  iframe src is built ONLY from a room id validated `^![A-Za-z0-9._=/+-]+:localhost$`
  and URL-encoded — never from free text (mirrors M-4/H-4 discipline).
- **Directory search**:
  - Local: filter the already-loaded room list by name/handle substring
    (client-side; no new fetch).
  - Remote "start chat" (WhatsApp only, canStartChat:true): for a USER-TYPED
    handle, the hub sends WhatsApp `start-chat <handle>` in the WhatsApp
    MANAGEMENT room after mgmt-room verification; the typed value goes as a
    command argument via the existing hub command path; the hub never builds
    a handle from remote/bridged content. iMessage's control is disabled and
    sends nothing.

## Security constraints (deltas only; hub A1/A2 + M-* stand)
- **U-1** Element iframe navigation: `src` set only to
  `http://127.0.0.1:8009/#/room/<validated,encoded room id>`; room ids come
  only from Matrix state the hub fetched (joined rooms / space children),
  never from typed or bridged text. No `javascript:`/`data:` ever (Trusted
  Types + the constant origin prefix enforce).
- **U-2** All rendered strings (room names, member names, search previews,
  last-message snippets) are bridged/remote content → sanitized + textContent
  only (H-5/H-15), length-clamped, never innerHTML, never used to build a URL.
- **U-3** "Start a chat" issues a bot command with the typed value as an
  argument, only after mgmt-room verification (H-6), only to a source whose
  mgmt room resolves; disabled while a send is in flight; the hub does not
  parse or trust the bot's echo of that value.
- **U-4** No capability/scope increase: the hub still holds only @jkali's
  client token (H-17), reads rooms the user is in, and drives the same bot
  commands already permitted. Directory adds no new privilege.
- **U-5** Left-bar source config is a static array in app.js; adding a source
  is a code edit, not a data-driven load — no remote/bridged input selects
  what the sidebar renders or which space it reads.

## Slices
- **P3.1** — Sidebar shell: replace the top-tab bar with a left sidebar
  (Sources / Directory / Connections / Settings); "All" = current embedded
  Element (unchanged); Connections + Settings move into the sidebar intact.
  *Acceptance*: hub loads, sidebar renders, All/Connections/Settings all work
  as before; no new HTML-string sinks; CSP/headers unchanged (curl).
- **P3.2** — Per-source conversation lists: SOURCES array (All, WhatsApp,
  iMessage); each source tab lists its space's rooms (name + last activity);
  clicking one navigates the Element pane to that room.
  *Acceptance*: WhatsApp tab lists WhatsApp rooms, iMessage tab lists iMessage
  rooms; clicking a room opens it in Element (pane shows that room); iframe
  src is a validated/encoded room-id URL (code + observed); a room id failing
  the regex is refused; AND a space-child room absent from /joined_rooms is
  excluded from both the list and navigation (D-5).
- **P3.3** — Directory: unified search box; local filtering across all
  spaces with source badges; per-source "start a chat" that drives the bridge
  bot in its mgmt room. *Acceptance*: typing filters existing convos across
  sources; a start-chat for a test handle drives the correct bot mgmt room
  (verified) and no command fires for an unresolved/again-in-flight state;
  the iMessage start-chat control is disabled and fires zero bot commands
  (D-1); a bridged/non-mgmt room in the widened sync set produces no console
  or command dispatch and never drives the connection-status parser (D-3);
  remote strings render via sanitizeLine + textContent.
- **P3.4** — Verification: fresh pilotfish:verifier on the exact claim
  (sidebar with All/WhatsApp/iMessage/Directory/Connections/Settings; source
  tabs list the right spaces; clicking opens the room in Element via a
  validated URL; a space-child not in /joined_rooms is neither listed nor
  navigable (D-5); Directory local filter works and the WhatsApp start-chat
  drives its mgmt room while iMessage's is disabled (D-1); a bridged non-mgmt
  room yields no console/command dispatch and no status-parser ingestion
  (D-3); no HTML-string sinks; hub CSP/headers and frame chain unchanged;
  token still sessionStorage; standalone stack unaffected).

## Rollback
Single surface: revert hub/site/{index.html,app.js,style.css} to the A2 state
(git-free, but the files are the only change); restart hub container. No
homeserver/daemon/registration changes in Phase 3 at all.

## Stops
Any slice failing after 2 distinct fix attempts → stop and report. No engine-
visible sends except a single self-directed test in P3.3 if needed (M-15
caps). Never navigate the Element pane to a non-127.0.0.1:8009 origin.

## Security review dispositions + model routing (2026-08-26) — GOVERNING
Verdict: P3.1/P3.2 sound; P3.3 Directory reshaped by D-1 (iMessage has no
start-chat and M-2 forbids adding one). Dispositions:
- **D-1 (P2)** Directory "start a chat" is a per-source static capability
  flag in the SOURCES array: `canStartChat:true` for WhatsApp (existing gated
  path, no new capability), `false` for iMessage → disabled "not available
  yet". Adding iMessage create-chat is a SEPARATE future amendment (widens
  M-18 blast radius) — not built here.
- **D-2 (P2)** start-chat accepts ONLY a user-typed handle; search results are
  display-only (or, if clickable, re-validated against a strict handle regex
  and shown verbatim in a confirmation before send).
- **D-3 (P2)** Sidebar sync must not widen the command path: explicit
  `if (roomId !== mgmtRoomId) continue;` at console/command dispatch + per-
  source bot-mxid checks. Bridged text never reaches the console or the
  connection-status parser.
- **D-4 (P3)** iframe-src safety is from CSP `frame-src http://127.0.0.1:8009`
  + constant absolute prefix + room-id regex (NOT Trusted Types, which does
  not govern iframe.src). Verify Element's route parser vs encodeURIComponent
  of `:`/`!`; if it fails, validate-then-concatenate without encoding (safe
  given the charset excludes #,?,%,\,whitespace,controls).
- **D-5 (P3)** Intersect space-hierarchy children with /joined_rooms before
  render/navigate (bridge-written m.space.child is untrusted).
- **D-6 (P3)** No CSP change EXCEPT avatars: either omit avatars in P3.2 or
  reuse the authenticated-fetch→Blob pattern with revokeObjectURL on teardown.
- **D-7 (P3)** Add `sanitizeLine()` (strip \n\r\t, clamp ~64) for rows/badges/
  previews; single-line overflow; never linkify remote text.
- **D-8 (P4)** iframe.src reload per navigation is accepted (do NOT replace
  with postMessage/widget-API — that is a new trust boundary).
- **C-1 (shared)** hub `sendCmd` mgmt-room verification becomes UNCONDITIONAL.

### Model routing (cyber flagging)
SECURITY-FLAGGED → pilotfish:security-executor (Opus), NOT Fable/main:
- **U-1 / D-4 / D-5** iframe-navigation validation and joined-rooms
  intersection (validation/hardening against URL injection + untrusted
  space state).
- **U-3 / D-1 / D-2** start-chat gating, capability flag, handle validation
  (authz + input validation; the WhatsApp send path already exists but the
  new UI trigger + validation is security-sensitive).
- **D-3** command/console-path isolation in the widened sync loop (authz).
- **D-7** sanitizeLine for the untrusted-content rendering contract (validation).

NON-SECURITY → main (Fable) or pilotfish:executor (Sonnet):
- **P3.1** sidebar shell layout / tab restructure (Connections+Settings move
  intact).
- **P3.2** conversation-list layout and the SOURCES array wiring (the
  *rendering* of room names uses the D-7 sanitizer, which is security-owned;
  the layout is not).

P3.4 verification is a fresh pilotfish:verifier (Opus) pass, unchanged.

### P3 execution outcome (2026-08-26)
Sidebar HTML/CSS scaffold built by Fable (main); security-critical app.js
(nav model, per-source lists, Directory, iMessage card) built by
pilotfish:security-executor (Opus). Verified by Fable: node --check passes; 0
HTML-string sinks; ROOMID_RE = ^![A-Za-z0-9._=/+-]+:localhost$; all 3 iframe
.src writes are constant or validated-roomId (U-1); verifyImsgMgmt/
resolveImsgMgmt (B-2 marker-based), routeMgmtEvent (D-3 isolation),
sanitizeLine (D-7), canStartChat gate (D-1), validHandle (D-2), unconditional
verifyMgmt (C-1) all present; hub CSP/XFO/COOP byte-identical to pre-P3; all
referenced DOM ids resolve (6 built dynamically as before). Data layer:
iMessage space (2 joined children) + WhatsApp space "WhatsApp (+1...)" (288
children) resolve. Fable functional fix (not security): space match changed
from name=== to name.startsWith(spaceName) so the mautrix "WhatsApp (+num)"
space populates its tab — D-5 joined-rooms intersection still governs
navigability, so this is not a security control. Browser-only visual behavior
(sidebar render, click-to-open) is user-checklist (Chrome extension
unavailable — accepted deviation class).
