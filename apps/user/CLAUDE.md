# apps/user/ — the teammate app

The full per-teammate hub: everything `shared/` provides, plus the sharing
controls, consent panel, proposal inbox, and contact management that make
this teammate's instance the *source* side of master-sync (PLAN-MASTER-SYNC.md
§5, §12 phase 5, §2 v2). This is the only app with a composer / send path.

## What lives here

- `index.html` / `main.js` — the app shell (session, sign-in, nav wiring,
  the bridge status console) plus `enterApp()`, which is the one place that
  calls each feature's `init*UI()` entry point
  (`initConsentUI`/`initProposalsUI`/`initContactsUI`) after sign-in, each
  wrapped in its own `try/catch` so one feature failing to initialize
  degrades that feature only (share controls stay at safe defaults on
  error; the proposals/contacts hooks simply stay unregistered).
- `invites.js` — the **bridge-invite trust predicate leaf**: `localpart`,
  `bridgeInvitesToJoin`, `ROOM_SHAPE_RE`. Zero imports, no DOM, no network,
  no side effects — importable by plain node, which is how
  `tests/unit/user_invites.test.js` holds every trust decision still. It only
  ever *decides*; `main.js`'s `joinBridgeInvites()` performs. Same contract as
  `apps/master/invites.js` (see `apps/master/CLAUDE.md` and
  `docs/SHARE-LOGIC.md` for the shared rationale).
- `consent.js` — PLAN §5.1/§4.2. Reuses `shared/model/consent.js` for all
  resolution + storage; wires into `shared/ui/rows.js` (`setConvoRowDecorator`
  → per-row badge + tri-state Share/Auto/Private toggle), `shared/ui/search.js`
  (`setSourceViewHook` → per-source "Share all `<source>`" switch), and
  `shared/ui/nav.js` (`setSharingViewHook` → the global Share-All switch +
  the consent summary panel). The panel's "newly auto-shared" flagging (§4.2
  guard 2) uses a `localStorage` "seen" set — **convenience state only**; the
  actual authorization decision always comes from `resolve()`/`resolveAll()`,
  never from what has or hasn't been flagged.
- `contacts.js` — PLAN §12 phase 5. The *only* place that renders
  `com.jkali.contact_profiles`: create/rename/delete a profile, attach/detach
  conversations to it (client-side filter over already-loaded
  `convosBySource`, no new endpoint), a per-profile Share/Auto/Private
  toggle, and non-auto merge suggestions (`suggestions()` from
  `shared/model/contacts.js` — advisory only; a "Create contact" click is
  the only thing that ever turns a suggestion into a real link).
- `proposals.js` — PLAN §2 v2 / §7. Reads the teammate's dedicated local
  proposals room (created by the uplink, marked `com.jkali.proposals`) and
  renders each `com.jkali.proposal` event as a clearly-flagged **DRAFT** card
  in its own inbox region (`#proposals-list`) — **never** through
  `renderMessageEvent`, so a proposal can never be mistaken for a real
  message and the from_me anti-spoof gate is untouched. Actions: "Send to
  conversation" (calls the guarded `sendConvoMessage(targetRoom, body)` with
  an *explicit* target — never falls back to whatever's open), "Edit in
  chat" / "Use in composer" (prefills the composer for the teammate to send
  themselves), "Dismiss" (marks handled locally, never sends).
- `main.js`'s `sendConvoMessage(targetRoom, bodyOverride)` call sites are the
  **only** two ways a message leaves this app: typing + Send/Enter in the
  open conversation, and approving a proposal. Both go through the same
  guarded function in `shared/ui/chat.js`. **That is a statement about this
  app, not about the machine:** for a conversation the teammate has set to
  the `direct` level, `agents/uplink/` sends manager proposals into the
  conversation itself, with no click here (see `agents/uplink/CLAUDE.md`'s
  gate list). This app is where the teammate opts into that — a separate
  confirm, never a cycle position — and where the resulting records are
  rendered: a `com.jkali.auto_sent` proposal is non-actionable history
  ("Sent directly"), a `com.jkali.send_ambiguous` one is the labelled "may
  already have been sent" row, and both are classified **from event content
  only**, never from `localStorage`. Neither is ever sendable with one click.
- `style.css` — app-specific styling for the share controls, proposal
  cards, and contact cards (shared layout/typography lives in `shared/`'s
  CSS, loaded by `index.html`).

## Security invariants (do not weaken)

- **Bridge invites are auto-joined ONLY through `invites.js`'s identity-bound
  gate.** The six bridges create a room per conversation and *invite* the user
  (only Google Messages double-puppets and joins on the user's behalf), so
  without this gate every other bridge's conversations stay invisible.
  `joinBridgeInvites()` in `main.js` accepts an invite iff
  `bridgeInvitesToJoin()` returns its id, and that predicate requires **two
  independent server-stamped fields to agree**: the room's `m.room.create`
  `sender` and the *single* sender of the `m.room.member` invite addressed to
  this user must be the same account, and that account must be one of the six
  code-owned `SOURCES[].botMxid` bots. Multiplicity (two different inviters),
  a bridge **ghost** (`@gmessages_abc:localhost`), the user, or any other local
  account fails closed. A **space** invite carries a third bind: its name must
  start with the `spaceName` of the same source whose bot created it — without
  it, one bridge's bot could present a space that
  `shared/ui/account-data.js`'s `buildConvos()` (which selects a source's space
  by name prefix alone) would read as another source's. DMs *and* groups are
  accepted (deliberately no `is_direct` filter; stripped invite state does not
  carry it anyway). Joins are capped per pass (30) and per session (200),
  hard (non-429 4xx) failures are memoized in a session-scoped `Set` and never
  retried, and joining is membership — not a send, and not a capability grant.
- **`invites.js` stays a pure zero-import leaf and the single definition of
  these predicates.** Never add an import, DOM access, network call, or a
  fallback/sentinel return value to it, and never sanitize *inside* a
  predicate. Do not re-implement any of its checks in `main.js` — where
  `main.js` needs to know which bridge an admitted invite came from (for the
  confirm's count), it re-runs the same predicate restricted to one bot rather
  than parsing invite state itself.
- **First-run consent confirm.** The first time this app would accept invites
  (per browser profile, flag `beepa_autojoin_ack` in `localStorage`), it asks
  first and states how many of those rooms would become **visible to the
  manager** under the current sharing policy. That count comes from the shared
  resolver (`consent.js`'s `countSharedNow()` → `resolveAll()`), never from a
  hand-rolled rule; it is a prompt, never an authorization decision. Declining
  leaves the invites pending. The `localStorage` flag is per-viewer
  convenience only — it gates the *prompt*, never the identity gate.
- **Refusals are visible, not silent.** Invites refused on identity grounds,
  deferred by the per-pass cap, or left pending by a declined confirm are
  counted and rendered as "N pending invitation(s) not accepted"
  (`#autojoin-note`, `textContent` only), with the escape hatch (review them
  in Element) in its `title`.
- **`sendConvoMessage()` (in `shared/ui/chat.js`) is the ONLY external send
  path in this app — full stop.** It re-validates the target room at send
  time (`ROOMID_RE` ∩ `feedModel` ∩ `S.joinedSet`) and explicitly refuses
  the six bridge management rooms. Nothing in `consent.js`, `contacts.js`,
  or `proposals.js` adds a second send path or calls `PUT
  /send/m.room.message` directly — they all funnel through this one
  function. If you add a new feature that needs to send a message, call
  `sendConvoMessage`, never re-implement the guard.
- **A proposal is a suggestion, never an instruction to send.** `proposals.js`
  never auto-sends: every action requires the teammate to press a button, and
  "Send" still goes through the same guard as typing.
- **textContent-only, no CSP change.** `consent.js`/`contacts.js`/`proposals.js`
  build every node with `el()`/`sanitizeLine()`/`textContent`; none of them
  loosens `apps/user/index.html`'s CSP (`script-src 'self'`,
  `require-trusted-types-for 'script'`, `object-src 'none'`,
  `frame-ancestors 'none'`, `connect-src 'self' http://127.0.0.1:8008`). The
  policy carries **no `frame-src`**: the embedded Element pane was removed and
  Element demoted to an opt-in escape-hatch container (docker-compose profile
  `escape`, no longer on the daily path). `apps/master/index.html`'s CSP now
  differs only by adding `media-src` (v1.5 media) — see `apps/master/CLAUDE.md`;
  if you ever touch `apps/user/index.html`'s CSP, diff both files, and keep it
  byte-identical to the copy in `views/nginx.conf`
  (`tests/unit/csp_parity.test.js` asserts that).
- **`localStorage` state (`SEEN_KEY` in consent.js, `IGNORE_KEY` in
  contacts.js, `HANDLED_KEY` in proposals.js) is per-viewer convenience only
  and is never trusted for authorization.** The truth is always re-derived
  from account-data / the live consent resolver / the live proposals room.
- **Consent is always read from `shared/model/consent.js`.** Never
  hand-roll the precedence logic in this app; call `resolve()`/`resolveAll()`.
- **A profile links a room; it never shares one.** Conversation sharing is
  EXPLICIT-ONLY since the direct-share-level plan's D1: the per-conversation
  level (`share`/`direct`/`private`, absent-or-unrecognized = private) is the
  whole decision, and a contact profile's `share` field no longer affects
  conversation mirroring at all. That resolution lives in the shared
  resolver, not here — `contacts.js` only sets the *profile* level's `share`
  field via `setProfileShare()`.
- **`direct` is never reachable by a pass-through tap.** The share cycle goes
  `share → private` only; `direct` has its own control behind an explicit
  confirm whose copy states that manager messages will be sent as the
  teammate without review, that a master/manager compromise can send as them,
  and that recipients cannot tell the difference. The per-source bulk action
  offers `share`/`private` only — never `direct`.

## How to run / test

The app's own pure logic — the invite trust gate — is unit-tested:

```bash
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
  node tests/unit/user_invites.test.js     # also wired into tests/run.sh
```

Everything else it depends on (`shared/model/consent.js`,
`shared/model/contacts.js`) is tested where it lives; see `shared/CLAUDE.md` /
`tests/CLAUDE.md`. To exercise the app live:

```bash
# bring up the existing single-user hub stack (bridges + local Synapse):
docker compose up -d          # from repo root; see docker-compose.yml
# then open apps/user/index.html against that stack (served, not file://,
# so the CSP/Trusted-Types + relative-module-import behavior matches prod)
```

The **integration harness** (`tests/integration/harness.py`) is the real
end-to-end coverage for this app's share controls + proposal inbox +
contacts, since it drives the uplink against a real local + master
homeserver pair and asserts on both sides — see `tests/CLAUDE.md`.

## How to change this safely

1. Any new UI feature that needs to read/change sharing state must go
   through `shared/model/consent.js` / `shared/model/contacts.js` — never
   read/write `com.jkali.share_policy` / `com.jkali.share_override` /
   `com.jkali.contact_profiles` account-data directly from this app.
2. Any new "write" surface (a button that sends something) must call
   `sendConvoMessage` from `shared/ui/chat.js` with an explicit target —
   do not add a second code path that PUTs `/send/m.room.message`.
3. Register new app-specific view logic via a `set<X>Hook()` in the
   relevant `shared/ui/*.js` file rather than editing shared code to know
   about `apps/user/` directly (see the existing hooks in `nav.js`/`rows.js`/
   `search.js`).
4. If you change `apps/user/index.html`'s CSP, treat it as
   security-sensitive: re-diff against `apps/master/index.html`'s CSP and
   confirm nothing that should be tighter for master got loosened for user
   by mistake (or vice versa).
5. Re-run `tests/unit/consent.test.js` / `consent_py.test.py` after any
   consent-adjacent change, and the relevant integration scenarios
   (`1_share_one_conversation`, `5_share_all_standing_policy`,
   `6_revoke_each_level`, `10_proposal_down`, `11_profile_span_platforms`)
   after any change touching sharing, proposals, or contacts.
