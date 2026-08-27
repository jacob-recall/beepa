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
  guarded function in `shared/ui/chat.js`.
- `style.css` — app-specific styling for the share controls, proposal
  cards, and contact cards (shared layout/typography lives in `shared/`'s
  CSS, loaded by `index.html`).

## Security invariants (do not weaken)

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
  `frame-ancestors 'none'`, `connect-src 'self' http://127.0.0.1:8008`, plus
  `frame-src http://127.0.0.1:8009` for the embedded Element pane).
  `apps/master/index.html`'s CSP is a strict subset of this one (no
  `frame-src`, has `media-src`) — see `apps/master/CLAUDE.md`; if you ever
  touch `apps/user/index.html`'s CSP, diff both files.
- **`localStorage` state (`SEEN_KEY` in consent.js, `IGNORE_KEY` in
  contacts.js, `HANDLED_KEY` in proposals.js) is per-viewer convenience only
  and is never trusted for authorization.** The truth is always re-derived
  from account-data / the live consent resolver / the live proposals room.
- **Consent is always read from `shared/model/consent.js`.** Never
  hand-roll the precedence logic in this app; call `resolve()`/`resolveAll()`.
- **A profile links a room; it never bypasses per-conversation `private`.**
  The 4-level precedence (override > profile > source > global) is enforced
  in the shared resolver, not here — `contacts.js` only sets the *profile*
  level's `share` field via `setProfileShare()`.

## How to run / test

There is no standalone unit-test file specific to this app (the pure logic
it depends on — `shared/model/consent.js`, `shared/model/contacts.js` — is
tested where it lives; see `shared/CLAUDE.md` / `tests/CLAUDE.md`). To
exercise it live:

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
