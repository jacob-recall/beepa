# apps/user/ — the teammate app

The full per-teammate hub: everything `shared/` provides, plus the sharing
controls, consent panel, manager-draft ghost (no inbox), and contact management that make
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
  `com.jkali.contact_profiles`, and the home of the profile-level per-contact
  share fan-out (one `com.jkali.contact_overrides` key per linked handle): create/rename/delete a profile, attach/detach
  conversations to it (client-side filter over already-loaded
  `convosBySource`, no new endpoint), a per-profile Share/Auto/Private
  toggle, and non-auto merge suggestions (`suggestions()` from
  `shared/model/contacts.js` — advisory only; a "Create contact" click is
  the only thing that ever turns a suggestion into a real link).
- `proposals.js` — PLAN §2 v2 / §7, Triage Rail form (2026-10-03). Reads the
  teammate's dedicated local proposals room (created by the uplink, marked
  `com.jkali.proposals`). There is **no inbox tab**: each room-targeted
  `com.jkali.proposal` becomes an entry in that room's feed record `drafts`
  (`attachDrafts`, pure), and the OPEN room's drafts render as a **ghost** in
  `#convo-ghost` above the composer — **never** through `renderMessageEvent`,
  so a draft can never be mistaken for a real message and the from_me
  anti-spoof gate is untouched. A draft is PENDING only while
  `draftPending(origin_ts, room.lastTs)` from `shared/model/attention.js`
  holds (fail closed: unknown activity ⇒ not pending); any later message from
  anyone retires it visibly (struck through, "Restore" for 24h). The chat list
  shows the same state as a violet stripe/pill and a `Draft:` preview
  (`shared/ui/rows.js`). Ghost actions: "Send as me" calls the guarded
  `sendConvoMessage(targetRoom, shown, { fromProposal })` with an *explicit*
  target and the **exact string shown** (`sanitize(body)`, F14 — never a
  truncated preview of a longer send); "Edit" prefills the composer; "Dismiss"
  marks handled locally. Person-targeted (new-chat) drafts render as a
  pseudo-row at the top of the Home list and keep the detail pane + verbatim
  confirm + gated `start-chat` leg. `parseProposal` takes
  `min(origin_server_ts, content.origin_ts)` (F17: the manager controls
  `origin_ts`; a far-future value must never pin a ghost as pending).
  `com.jkali.from_proposal` is a cosmetic content stamp read only by the
  manager console's suggestion stack (and only on from_me messages); no trust
  logic anywhere reads it.
- `main.js`'s `sendConvoMessage(targetRoom, bodyOverride, meta)` call sites are the
  **only** two ways a message leaves this app: typing + Send/Enter in the
  open conversation, and "Send as me" on a ghost draft. Both go through the same
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
- `scheduled.js` — F7 teammate self-schedule. The composer's clock button and
  ⌘⏎ open a NATIVE `<input type="datetime-local">` (min now+1min, max now+30d);
  confirming writes a `com.jkali.scheduled_send` `{target_room, body, send_at,
  created_by}` into the teammate's own local proposals room. **This app never
  fires a schedule and never sends one** — `agents/uplink/` does, behind its own
  gates. Cancel is a `com.jkali.scheduled_cancel` STATE event whose `state_key`
  IS the scheduled event id, which is what makes the daemon's fire-time
  point-read a single unambiguous question. The ghost cluster shows scheduled /
  held / sent / refused items, and "Send now" on a held one goes through
  `sendConvoMessage` with the re-read body and then cancels the schedule.
  **Classification (`classifySchedules`) is from event content ONLY** — the
  scheduled_send, the cancel state event and the daemon's
  `com.jkali.scheduled_outcome` records — never `localStorage`, so a fresh
  profile and a poisoned one agree. `scheduleRefusal()` is the pure write-time
  gate and refuses what the daemon would refuse later (a leading `!`, a
  non-attributed or management-room target, a time outside the window); the
  4000-char `sanitize()` clamp applies to what is written. The clock-skew
  refusal (>5 min vs the server's `Date` header) is **UX, not a security
  boundary**: the daemon re-checks everything at fire time, and an unreadable
  server clock does not block scheduling.
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
  retried, and joining is membership — not a send; under an account default of `direct` it is, however, enough for the uplink to mirror and auto-send into that conversation (see `agents/uplink/CLAUDE.md`).
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
  manager** under the current sharing settings. When the ACCOUNT DEFAULT is not
  private it also says so in words ("…will be Direct: your manager can send as
  you in them"), because under a default, accepting an invite is no longer
  membership-only. That count comes from the shared
  resolver (`consent.js`'s `countSharedNow()` → `resolveAll()`), never from a
  hand-rolled rule; it is a prompt, never an authorization decision. Declining
  leaves the invites pending. The `localStorage` flag is per-viewer
  convenience only — it gates the *prompt*, never the identity gate.
- **The account-data twin is the DAEMON's gate, and it is a real consent
  record.** An affirmative confirm ALSO writes user account-data
  `com.jkali.autojoin_ack` = `{ok:true, ts}` (`writeAutojoinAck()`), and that
  event is what `agents/uplink`'s `invites` stage requires — fail-closed,
  `ok` must *be* `true` — before it joins anything while this app is closed.
  Never write it on a decline and never on render. Existing installs that
  confirmed before the daemon existed are migrated once by
  `migrateAutojoinAck()` (localStorage says acked AND the account-data GET is
  404 → write it); any other error leaves it absent, so those installs simply
  stay joined-by-browser-only. The two flags have different jobs: the
  `localStorage` one gates the *prompt* in this browser profile, the
  account-data one authorizes *unattended* joining by the daemon.
- **Mgmt-room resolution runs before `joinBridgeInvites()` only as an
  optimization** (`resolveMgmt` does a GET per joined room, so joining first
  enlarges that scan). Ordering is NOT what keeps a session secret out of a
  bridge portal — it cannot be, now that the uplink joins invites while this
  app is closed. That guarantee is `isBotDmMgmt()`'s full-state check in
  `shared/ui/sources.js`: a portal carries `uk.half-shot.bridge` and a source
  space is an `m.space`, and either marker refuses the room (plus
  `verifyImsgMgmt()`'s marker-and-not-a-portal test for iMessage).
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
- **A schedule is an INTENT RECORD, not a send.** `scheduled.js` adds no send
  path: its only writes are a `com.jkali.scheduled_send` timeline event and a
  `com.jkali.scheduled_cancel` state event, both into the discovered proposals
  room and never into a conversation. "Send now" on a held schedule is the
  ordinary `sendConvoMessage` guard with an explicit target. A manager's timed
  suggestion is still only a draft here — "Accept schedule" writes the
  TEAMMATE'S own scheduled_send, so from then on the daemon is acting on their
  intent, not the manager's.
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
- **The consent-write invariant (per-contact-share plan, F8): no consent
  control may swallow a write error.** A failed write is SURFACED next to the
  control, and the control keeps rendering **last-known-good** state — never
  the requested state. A toggle that looks moved but never landed is a consent
  lie, and it was the reported bug in the contact-share affordance. This
  applies to `buildTriStateSlider`'s handler, the header share chip
  (`headerChip`, Triage Rail), both global switches, the per-contact
  controls, and the profile fan-out. A handler may still return
  `false` to mean "refused deliberately" (a declined confirm) — that is not a
  swallowed error, and the refusing surface says so itself.
- **Every consent write is a MERGE over a FRESH read, and a failed read writes
  NOTHING (F3).** `applyContactOverrides` (per-contact overrides) and
  `saveProfilesGuarded` (`com.jkali.contact_profiles`) are the only two write
  paths; both re-read first, both distinguish 404 (empty) from any other
  failure (controls disabled, zero PUTs), and neither ever blind-PUTs this
  module's cache. `contacts.js`'s `persist()` therefore takes a MUTATOR that
  receives the freshly-read store, not a pre-computed one.
- **The per-contact override entry cap (1024) is refused BEFORE any PUT (F5),
  with a visible message naming the cap.** A stored map already over the cap
  reads as a read failure, but the destructive-only writes — single-key
  removal and clear-all — stay permitted in that state, so recovery never
  needs a raw Matrix client.
- **A profile fan-out never mints a key that would not apply (F7).** It writes
  one override key per LINKED handle, but only for handles that pass the key
  spec + known-source gate AND reconcile against `POST /contacts/list`;
  anything else is reported visibly ("2 of 3 handles applied; …"). A `'private'`
  that silently never applies is a leak the teammate believes closed, and a
  `'share'` on a not-yet-imported handle is a dormant grant.
- **Retraction copy must stay honest (F2).** Turning a contact off stops
  sharing it and removes it from the manager's list; it does NOT un-send what
  was already mirrored — tombstones are state events, so prior content stays
  readable from room history to anyone already joined. Never restore copy that
  claims removal deletes what was shared.
- **The ACCOUNT DEFAULT (roadmap §6) is the one standing setting that shares.**
  `com.jkali.share_policy.default_level` (`private|share|direct`, absent ⇒
  private) applies to every conversation the holder has NOT set individually —
  the ones they have now and the ones that arrive later. An explicit
  per-conversation level always wins, in both directions. The UI must say
  "conversations you haven't set individually", never "new conversations", and
  the "default" marker on a chip/row is driven by the RESOLVER'S REASON
  (`default-share`/`default-direct`/`private`), never re-derived locally.
  Setting the default to Direct goes through the same risk confirm as a
  single conversation, worded for its real scope. The control is DISABLED, with
  a stated reason, when the policy read failed (`policyStatus === 'error'`) or
  the daemon has not yet announced consent model 3 — a UI that offered a
  default an older daemon does not implement would be claiming a sharing state
  the enforcer has not got. Resolution itself stays default-aware at every
  marker version, because over-claiming exposure is the safe direction.
  **A non-private default is advertised by a persistent, NON-DISMISSIBLE banner
  above the chat list** (`#default-level-banner`), so the holder can always see
  it and revert it without a raw Matrix client. Never add a dismiss control.
- **Deploy order for the account default: APP FILES FIRST, daemon second.**
  The consent-model marker moves 2 → 3 ("per-room override, else the account
  default"), and the daemon writes it (`ensure_consent_model_marker`, once per
  version, even on an already-migrated install). New app over old daemon is the
  safe skew: the app resolves defaults and OVER-claims exposure while the
  daemon mirrors nothing extra, and the control stays disabled with "waiting
  for the sync service to update" until the marker reaches 3. Old app over new
  daemon is the unsafe skew and is the honest residual here: **a browser tab
  left open on the previous app build keeps showing "Private" for conversations
  the updated daemon is already sharing under the default.** Nothing in the app
  can fix that — the fix is to reload the app after a daemon update, and never
  to ship the daemon first.
- **Clearing an override is not the same as making a room private.** An unset
  override means "take the account default", which under a share/direct
  default RE-SHARES the room. Every surface whose intent is "stop sharing this"
  writes `'private'` explicitly — including the summary panel's "Shared but not
  mirrorable" Clear button.
- **The bulk "set every conversation to Direct" sweep writes EXPLICIT
  overrides.** Its confirm carries the total count, the per-source breakdown,
  the verbatim Direct risk copy, every affected conversation by name in a
  scrollable region, the sentence that changing the default back later will NOT
  un-Direct them, and a typed `DIRECT` (`confirmModal`'s third argument takes
  the required word, not just a boolean). Writes go one room at a time through
  `writeShareOverride` and STOP at the first failure, reporting how many
  landed — a half-applied sweep the user believes complete is a consent lie.
- **A profile links a room; it never shares one.** Conversation sharing is
  per-conversation since the direct-share-level plan's D1: the per-conversation
  level (`share`/`direct`/`private`), else the account default, is the whole
  decision, and a contact profile's `share` field no longer affects
  conversation mirroring at all. That resolution lives in the shared
  resolver, not here — `contacts.js` only sets the *profile* level's `share`
  field via `setProfileShare()`.
- **`direct` is never reachable by a pass-through tap.** The share cycle goes
  `share → private` only; `direct` has its own control behind an explicit
  confirm whose copy states that manager messages will be sent as the
  teammate without review, that a master/manager compromise can send as them,
  and that recipients cannot tell the difference. The per-source bulk action
  offers `share`/`direct`/`private` — bulk `direct` was enabled by an
  explicit 2026-09-02 product-owner decision reversing the plan's original
  F11 disposition (see D3 in
  `docs/superpowers/plans/2026-09-02-direct-share-level.md`), and is gated
  behind a confirm that enumerates EVERY affected conversation by name,
  carries the same risk copy as the single-conversation confirm, and states
  that only existing conversations are affected. Every write of `'direct'`
  (single or bulk, including the header chip's adjacent "Direct…" button)
  still funnels through `writeShareOverride`/`escalateToDirect` — the cycle
  itself never reaches it. The header chip toggles Private ↔ Shared only;
  Direct → Private via the chip is a de-escalation and needs no confirm.

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
end-to-end coverage for this app's share controls + manager drafts +
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
