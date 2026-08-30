> ARCHIVED (2026-08-30): historical planning doc, superseded — kept for reference only.

# Amendment: unified Home feed (recent conversations across all platforms) — home-v1

User request (2026-08-26): the hub's home/landing view becomes a UNIFIED list of
the most-recent conversations across WhatsApp + iMessage, sorted by recency,
live-updating when any new message arrives, each row tagged with a small
platform logo on the side; clicking a row opens it in the embedded Element
pane where replying is identical regardless of platform.

Extends the Phase 3 hub (PLAN-IMSG3.md, GOVERNING U-1/D-3/D-5/D-7/C-1). Hub is
static, talks only to Matrix at 127.0.0.1:8008, embeds Element at :8009. NO new
backend/port/secret/CSP change. Security-flagged (live bridged-content read +
render) → pilotfish:security-executor (Opus) builds app.js; Fable verifies.
Layout/CSS by Fable.

## Outcome
- The sidebar's default item becomes **Home** (replaces "All chats" as the
  landing view; the embedded Element pane stays as the reading/reply surface,
  shown when a conversation is opened). The per-source tabs (WhatsApp,
  iMessage) and Directory/Connections/Settings remain.
- Home = one merged, de-duplicated list of the user's conversations from BOTH
  the WhatsApp space and the iMessage space (space children ∩ /joined_rooms,
  D-5), sorted by each room's last-message timestamp DESC.
- Each row: platform badge (WhatsApp vs iMessage) on the side, chat name,
  last-message preview, relative timestamp. Otherwise rows are visually
  uniform — the badge is the only platform distinction (user: "otherwise
  indistinguishable").
- LIVE: a new message in any bridged room bubbles that conversation to the top
  and updates its preview/time without a manual refresh.
- Click a row → `openConversation(roomId)` (U-1 validated) → Element pane
  navigates to that room → reading/replying is the normal Element flow (bridge
  routes the reply to the right network; identical UX both platforms).

## Security constraints (deltas; all Phase-3 dispositions stand)
- **H-1 (the load-bearing one — the D-3 risk realized)**: the Home live feed
  uses a SEPARATE /sync read path that is NEVER wired to the command/console
  path. Bridged room content (names, last-message bodies) read for the feed
  must not reach `handleMgmtEvent`, `logConsole`, `reactToBotReply`, the
  connection-status parser, or any command dispatch. The command sync
  (mgmt-room-only, per-bot-mxid, D-3) stays exactly as is; the feed sync is
  additive and read-only-for-display.
- **H-2 (D-7)**: every rendered string (chat name, last-message preview,
  badge label) passes `sanitizeLine` (strip \n\r\t + control/bidi, clamp ~64;
  preview may use a slightly longer clamp, still single-line) then textContent.
  No innerHTML-family sinks (Trusted Types). Never linkify or build a URL from
  bridged text.
- **H-3 (U-1/D-5)**: a row is shown/navigable ONLY if its room id passes
  ROOMID_RE AND is in /joined_rooms AND is a child of one of the known source
  spaces (bridge-written m.space.child stays untrusted). openConversation is
  the only navigation path; iframe.src is unchanged (constant base + validated
  room id).
- **H-4 (badges)**: the platform badge is derived ONLY from which source
  SPACE the room belongs to (static SOURCES mapping) — never from bridged
  content. Logo rendering must respect CSP `img-src 'self' blob:`: use an
  inline `<svg>` element (createElementNS, not an `img` with a remote/data
  src) or a CSS-styled badge with the source's color + `source.icon`. No
  external image, no `img src=http://…`.
- **H-5**: the feed sync must be bounded (paginate/limit; don't hold unbounded
  history in memory); last-message extraction reads only the latest timeline
  event per room, body clamped (H-2). No message bodies logged anywhere (the
  hub doesn't log, but keep no console.log of content).
- **H-7 (find existing chats)**: Home has a client-side search box that
  filters the merged conversation list by chat name (and last-message
  preview) as the user types — pure local filter over the already-loaded
  feed, sanitizeLine on display, no new fetch/capability, no bridged text
  reaching the command path. This is the "make existing chats easier to find"
  ask: recent convos are on Home by default, and the search narrows the full
  merged list. (The Directory tab's cross-source filter remains for the same
  purpose.)
- **H-6**: no capability/scope increase — the hub still holds only @jkali's
  token (H-17), reads rooms the user is already in, drives no new bot command.
  Home is read + navigate only.

## Slices
- **HM.1 (layout, Fable)** — add a `#view-home` section with a search input
  (`#home-search`) + `.convo`-style feed list (`#home-list`) and a
  `.plat-badge` style; rename the "All" nav item to "Home"; Home is the
  default post-sign-in view. (CSS/HTML only; no logic.)
- **HM.2 (app.js, Opus)** — build the merged feed: gather both spaces' joined
  children (reuse the existing snapshot path), extract per-room {name,
  lastEventBody, lastTs, sourceId}, merge + sort by lastTs desc, render rows
  (sanitizeLine + platform badge via H-4) into `#view-home`; wire row click →
  openConversation. Add a SEPARATE live /sync consumer (H-1) that, on a new
  m.room.message in any fed room, updates that room's last event + re-sorts +
  re-renders (bubble to top); strictly isolated from the command sync (H-1).
  Default nav → Home. Wire `#home-search` as a client-side filter over the
  merged list (H-7): typing narrows rows by sanitized name/preview substring;
  clearing restores the full recency-sorted list; the filter never issues a
  bot command or fetch.
  *Acceptance*: Home lists conversations from BOTH spaces merged + sorted by
  recency; a crafted bridged message (e.g. one matching the list-logins status
  regex, or containing bidi/newlines) rendered in a feed row does NOT reach
  the console or the connection-status pill and renders as a clamped single
  line (H-1/H-2); typing in the Home search filters the merged list live and
  clearing restores it (H-7); a space-child not in /joined_rooms is absent (H-3); each row
  badge matches the room's space, not its content (H-4); clicking a row opens
  that room in Element via the validated URL; node --check passes; 0
  HTML-string sinks; hub CSP byte-identical.
- **HM.3 (verify, Fable)** — confirm HM.2 acceptance: merged/sorted feed
  present; live bubble on a new message (drive one self-directed test message);
  D-3 isolation (bridged content never in console/status parser); sanitizeLine
  on rows; badge-by-space; headers unchanged; sinks clean.

## Rollback
Revert hub/site/{index.html,app.js,style.css} to the pre-home state; restart
hub container. No daemon/registration/homeserver change.

## Stops
Any slice failing after 2 fix attempts → stop and report. Engine-visible test
messages target the user's own self-chat only (M-15). Never navigate the
Element pane off 127.0.0.1:8009.

## Security review dispositions (2026-08-26) — GOVERNING
Sound; build HM.2 per these (all mitigate-now unless noted):
- **HF-1 (P1)**: TWO independent /sync loops. Keep startSync/routeMgmtEvent/
  handleMgmtEvent BYTE-UNCHANGED (its server-side `room:{rooms:[mgmt ids]}`
  allowlist is the primary D-3 control). Add a SEPARATE `startFeedSync()` whose
  handler references ONLY feed-model + render functions — no lexical path to
  any command/console function. Bridged content reaches only the feed handler.
- **HF-2 (P2)**: independent `feedSince`/`feedRunning`; set `feedRunning=false`
  in forgetSession. No shared sync state between loops.
- **HF-3 (P2)**: the live feed handler updates/bubbles ONLY roomIds already in
  the validated feed model (ROOMID_RE ∩ joinedSet ∩ known-source-space child).
  Unknown roomIds ignored; new portals picked up by a debounced refreshConvos()
  re-validation, never added directly from the live stream.
- **HF-4 (P2)**: preview ONLY from `m.room.message` with string `content.body`
  and msgtype ∈ {m.text, m.notice}; media msgtypes → a static msgtype label
  ("Photo"/"Video"/"Audio"/"File"), NEVER the bridged filename; edits → 
  `m.new_content.body` only; read `body` only, NEVER `formatted_body`;
  reactions/redactions/receipts/typing/state are NOT "last message". Then
  sanitizeLine → textContent; preview clamp ≤~100 single-line, muted style
  distinct from the name.
- **HF-5 (P2)**: one record per room {id,name,lastBody(clamped),lastTs,
  sourceId} (overwrite, no history); feed filter `timeline.limit` small (1–10);
  long-poll timeout ~25000 + 3s error backoff; coalesce renders (one timer/rAF
  per batch); cap rendered rows ~200 by recency (search filters the full model).
  No bodies to console.log.
- **HF-6 (P3, accept+strengthen)**: badge derived ONLY from source-space
  membership (never a per-room bridged field); de-dup a doubly-listed room by
  first SOURCES order. Space identity by name-prefix stays (display-integrity,
  not a security boundary — nav is ROOMID_RE ∩ joined-validated).
- **HF-7 (P3)**: badge is a CSS pill/dot (source color) + `source.icon` via
  textContent, OR inline `<svg>` via createElementNS (path+fill only, no
  `<image>`/external `<use>`, no `data:`). NO `<img>`, no `data:`/remote URL —
  keeps CSP byte-identical.
- **HF-8/HF-9 (P4 accept)**: Home search is a pure local filter (never builds a
  URL/command/nav); default→Home keeps mountChats() in enterApp so
  openConversation has an iframe; token/session/frame posture unchanged.

### Execution outcome (2026-08-26)
HM.1 layout (view-home, home-search, home-list, .plat-badge) by Fable; HM.2
feed logic by pilotfish:security-executor (Opus); verified by Fable.
- Static/isolation: node --check passes; 0 HTML sinks; 11/11 feed functions
  present; NO command/console symbol referenced inside any feed function
  (HF-1 two-loop isolation confirmed by code scan); startSync/routeMgmtEvent/
  handleMgmtEvent byte-unchanged; feedRunning/feedSince separate; formatted_body
  only in a comment (preview reads body only); media static labels present;
  hub CSP byte-identical.
- Data layer: a filtered /sync + space-child ∩ joined merge yields a
  cross-platform recency-sorted feed (sampled 13 rows: 10 WhatsApp + 3
  iMessage) with real names + text previews interleaved by time — proves Home
  populates from both platforms sorted by recency.
Browser-visual (feed renders, live bubble on new msg, search filter, badge
colors) is user-checklist (Chrome ext unavailable — accepted deviation).

### Bug fixes (2026-08-26, post-deploy)
- **iMessage missing from feed** (Fable, functional): parseSnapshot read
  m.space.child only from the sync `state` block; the newer iMessage space
  keeps those in its `timeline` window, so state-only found 0 iMessage
  children. Fixed to scan state+timeline (deduped). D-5 unchanged (children
  still ROOMID_RE ∩ joinedSet gated). Verified: iMessage 0→3 children resolve.
- **"Element is not supported by the browser" on opening a conversation**:
  NOT our code — Element 1.12.26's own checkBrowserFeatures gate (bundle:
  "Browser is missing required features" + mx_accepts_unsupported_browser
  localStorage bypass). Tests recent features (Promise.withResolvers,
  Intl.Segmenter, secure-context, wasm). It offers a "continue anyway" link
  that sets the bypass flag (persists per-origin on 127.0.0.1). Diagnosis
  pending user's browser; options if it recurs: click-through (one-time on
  127.0.0.1), or pin an older/less-strict Element image.

### Home global-sort + declutter (2026-08-26)
- Global sort: bug-2 fix (parseSnapshot state+timeline) makes iMessage + WhatsApp
  (+ gmessages/meta once linked) merge into ONE Home list sorted by last message
  ts, sender-agnostic (sent OR received) — the user's ask. Sort logic unchanged.
- Declutter (Opus-built, Fable-verified): the feed now EXCLUDES rooms that are
  m.lowpriority-tagged (archived/low-priority), muted (a room push rule with no
  notify action), or manually hidden. Per-row "Hide"/"Unhide" sets/clears the
  m.lowpriority tag (validated roomId ∈ feedModel only); a "Show hidden" chip
  reveals them. Feed sync stays isolated (no command-path refs); node --check +
  0 sinks; CSP unchanged. WhatsApp config changed (archive_tag: m.lowpriority,
  mute_only_on_create:false, tag_only_on_create:false) so FUTURE mutes/archives
  auto-sync → auto-hide; existing state did NOT backfill (network doesn't push
  it retroactively), so existing archived/muted need one manual Hide (or a bridge
  re-sync). Live-verified: hiding "Oak Investment" set m.lowpriority → excluded.
  (Oak Investment left hidden per the user's explicit example.)
