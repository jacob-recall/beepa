# apps/master/ — the manager's read-only console

A separate, read-only Matrix client against the **master** homeserver
(PLAN-MASTER-SYNC.md §6.4). Structurally the user app with the top grouping
swapped `source → user`: recent feed across everyone, per-teammate spaces,
search across everyone, one-person-many-platforms grouping (unified
contacts) — and, as of V2, the single place the manager can write a
*proposal* (never a send). No composer, no send path, anywhere.

## What lives here

- `index.html` / `style.css` — the shell. **There is no message-composer
  input element anywhere in `index.html`** except the one dedicated
  proposal-compose input (`#proposal-input`), which is clearly labeled
  and writes only `com.jkali.proposal` (see below) — it is not, and must
  never become, a way to post `m.room.message`.
- `main.js` — everything else: sign-in, the master-homeserver snapshot
  loop, feed/teammate/search rendering, the read-only conversation viewer,
  media rendering (v1.5), unified-contacts grouping (§12 phase 5), and
  `submitProposal()` (v2, §2/§7).
- `invites.js` — the **trust-predicate leaf**: `localpart`,
  `spaceLabelFor`, `invitesToJoin`, `acceptedSpaces`, `verifiedChildIds`,
  `ROOM_SHAPE_RE`. Zero imports, no DOM, no network, no side effects —
  importable by plain node, which is how `tests/unit/master_invites.test.js`
  holds every trust decision still. It only ever *decides*; `main.js`
  performs. See `docs/SHARE-LOGIC.md` for the full rationale.

### Why this file does not import most of `shared/ui/`

`main.js`'s header explains this in detail; the short version: it imports
only `shared/matrix/client.js` (transport, no send side-effects of its own),
`shared/ui/el.js` (DOM helpers, no transitive imports), and `shared/state.js`
(the plain `S` session slot). It deliberately does **not** import
`shared/ui/render.js`, `rows.js`, `nav.js`, `chat.js`, `search.js`,
`sources.js`, or `connections.js` — every one of those is import-chained
into `shared/ui/chat.js`'s `sendConvoMessage` and/or `shared/ui/sources.js`'s
`sendCmd` at module-evaluation time (ES module imports execute eagerly), so
importing any of them would make the send path *present code* in this
app's bundle. PLAN-MASTER-SYNC.md is explicit that here "read-only" must be
**absent code, not a hidden button** — so `main.js` re-implements the small,
already-duplicated-per-read-path patterns those modules use (content
whitelist, recency sort, a tailing long-poll) locally instead:

- `resolveMirrorContent()` — a local, mirror-room-shaped equivalent of
  `shared/ui/render.js`'s `convoResolveContent()`: reads `content.body`
  only, never `formatted_body`; media gets a static label unless a v1.5
  re-uploaded master `mxc` is present (validated by the shared `MXC_RE`).
- `renderBubble()` — attribution from the *trusted* `com.jkali.from_me`
  flag. Unlike `apps/user`'s iMessage-bot sender check, no extra sender gate
  is needed here: master-side power levels (§8.3) already guarantee only
  `@<teammate>:master` can post into that teammate's own mirror room, so
  the flag cannot be spoofed by another party sharing the room.
- `startTail()` — a room-scoped long-poll tail, the master-app-local
  equivalent of `shared/ui/chat.js`'s `startConvoWatch`.
- `groupByProfile()` / `buildProfileGroup()` — reads the `com.jkali.profile`
  room-state stamp the uplink writes only on mirror rooms belonging to a
  *shared* contact profile, and clusters them under one header (§12 phase 5).
  Pure grouping over already-fetched data; adds no reads, no mutation.

If you refactor `shared/ui/render.js` or `shared/ui/chat.js`, re-check that
this file's local re-implementations still match the new shape — they are
now a deliberate, documented duplication, not an oversight.

## Security invariants (do not weaken)

- **No composer, no send call, anywhere in this file — except the one
  documented proposal write.** Never add a `PUT
  /_matrix/client/v3/rooms/{id}/send/m.room.message/...` call to this app.
  If a future feature seems to need it, it does not belong in `apps/master/`.
- **`submitProposal()` is the ONE message-shaped write path**, and it is
  deliberately narrow: the destination must be a `ROOMID_RE`-valid id present
  in the *discovered* proposals-room allowlist (`MS.proposalsRoomSet` — never
  a stale/typed id, never a mirror room); the event **type** is the hardcoded
  literal `com.jkali.proposal`; `target_room` (a foreign teammate-local room
  id) is shape-checked with `invites.js`'s `ROOM_SHAPE_RE` but the master
  never sends to it itself — it only records it for the teammate's own
  guarded send path to re-validate. This is a *suggestion* channel, not a
  reverse send path.
- **Proposal writes are pinned to the context captured at room-open.**
  `openRoom()` stores `MS.openProposalCtx = {mirrorRoomId, label,
  proposalsRoomId, targetRoom}`; `submitProposal()` re-asserts every element
  of that tuple against the *current* snapshot (mirror room still present
  under the same label, same proposals room still that label's discovered
  target and still in the allowlist, target still shape-valid) and writes to
  the **pinned** ids — it never re-resolves the destination. A mid-session
  revocation or label change therefore refuses the write instead of
  redirecting it somewhere new.
- **The write surface is exactly `{POST /login, POST /logout, POST
  /rooms/{id}/join, PUT /rooms/{id}/send/com.jkali.proposal/{txn}, POST
  ENROLL_BASE/admin/add-teammate, POST ENROLL_BASE/admin/delete-teammate}`** — asserted at build time by
  `harness.py`'s scenario 7, which scans **every `*.js` in this directory**
  (not just `main.js`) and matches each non-GET call against that allowlist,
  on the *concatenated string literals* of the path expression (no call in
  this codebase passes a single literal path). Adding any other non-GET
  endpoint fails the scenario.
- **CSP is tighter than `apps/user`'s**: no `frame-src` (no embedded
  Element pane here), `media-src 'self' blob:` added (for v1.5 re-uploaded
  media fetched as authenticated bytes and shown via an object URL — see
  `loadMediaInto()`), `connect-src 'self' http://127.0.0.1:8018
  http://127.0.0.1:8019` (the enroll/admin service for add-teammate; the master
  homeserver, not the user hub's 8008). If you touch this CSP, diff it
  against `apps/user/index.html`'s and confirm you have not accidentally
  widened it past user's, or dropped the `frame-src` exclusion.
- **Own homeserver, own module graph.** `configureMatrixBase({ csBase:
  'http://127.0.0.1:8018', serverName: 'master' })` repoints *this page's*
  copy of `shared/matrix/client.js` only — each HTML document gets its own
  ES module instance, so `apps/user` is never affected by this call.
- **Read-only is defense in depth, not the only boundary.** The real
  authorization boundary is (a) the teammate's own consent/uplink decision
  about what to mirror at all, and (b) master-side room power levels
  (`@manager` pinned to PL 0, `events_default` 50, in every mirror room —
  set by `agents/uplink/uplink.py`'s `create_mirror`) so even a modified
  master client cannot successfully send into a mirror room. This app's
  "absent code" property is the second, belt-and-suspenders layer.
- **Auto-join happens ONLY through `invites.js`'s identity-bound gate.**
  `joinPendingInvites()` accepts an invite iff `invitesToJoin()` returns its
  id, and that predicate binds every room to the account that created it —
  the server-stamped `m.room.create` **sender**, one of the few identity
  fields Matrix's *stripped* invite state actually carries. A space is joined
  only when its name is exactly `space:` + its own creator's localpart; any
  other room is joined only when it is a child of an already-joined,
  identity-verified space **and** its own create sender matches that space's
  label. The same bind is enforced again at render (`buildByUser` skips a
  child whose `createSender` localpart ≠ the space's label), so a mislabeled
  space is refused twice — that is what stops `@bob:master` naming a space
  `space:jkali`, injecting rows under jkali's label and hijacking
  `proposalsByUser`. Never gate on a custom state type: the old
  `com.jkali.proposals` marker gate could not fire on ANY invite, because
  stripped invite state never carries custom types — that was the live
  defect. There is deliberately **no rule that joins on a
  `com.jkali.mirror_of` shape alone** (it was the design's only
  unauthenticated join path). Joins are capped per pass, hard (non-429 4xx)
  failures are memoized in `MS.joinFailed` and never retried in-session, and
  joining is membership, not a send.
- **`invites.js` stays a pure zero-import leaf and the single definition of
  these predicates.** Never add an import, DOM access, network call, or a
  fallback/sentinel return value to it (an earlier `'unknown'` localpart
  sentinel was a fail-open hole), and never sanitize *inside* a predicate —
  a lossy transform makes distinct identities compare equal, so sanitizing
  belongs at the display call site, after the decision. Do not re-implement
  any of these checks locally in `main.js`.
- **Verification skips are visible, not silent.** Unverified spaces and
  refused children are counted in `MS.skippedUnverified` and surfaced in the
  sidebar as "N unverified item(s) hidden" (`#unverified-note`). A room
  discovered but not yet joined is a normal transient state and is not
  counted. A proposals-marked room that is *also* a mirror
  (`isProposals && mirrorOf`) never becomes a write target.

### Deferred hardening (filed, not in this change)

- `pm_mng-1mj` — verify the power-level *shape* of a discovered proposals
  room before treating it as a write target.
- `pm_mng-9fg` — disable presence on the master homeserver (a co-member can
  currently see the manager's presence).
- `pm_mng-o28` — server-side alternative: add the `com.jkali.*` markers to
  Synapse's `room_prejoin_state.additional_event_types` so they appear in
  stripped invite state (would allow direct recognition, but exposes the
  markers pre-join).

## How to run / test

```bash
# bring up + provision the master stack (separate from the live hub):
docker compose -p matrix-master --env-file master/.env \
  -f master/docker-compose.master.yml up -d
master/provision.sh
# then open apps/master/index.html served against that stack (127.0.0.1:8018)
```

Unit coverage for the trust predicates (fixtures copied from the real
stripped invite state):

```bash
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine \
  node tests/unit/master_invites.test.js     # also wired into tests/run.sh
```

End-to-end coverage lives in `tests/integration/harness.py`: scenario
`7_read_only_manager` asserts the manager cannot send (power-level
rejection) **and** that the composer is impossible; `10_proposal_down`
exercises `submitProposal()`'s guard end to end; `11_profile_span_platforms`
exercises `groupByProfile()`. See `tests/CLAUDE.md`.

## How to change this safely

1. Before importing anything new from `shared/ui/`, check whether it is
   import-chained to `chat.js` or `sources.js` (most of `shared/ui/` is —
   see the file header). If it is, re-implement the small piece you need
   locally instead, the way `resolveMirrorContent`/`renderBubble`/
   `startTail` already do, rather than pulling the send path in transitively.
2. Never add a second write path. If a new manager-facing feature seems to
   need one, it should almost certainly be another `com.jkali.*` proposal-
   shaped event into an allowlisted room, following `submitProposal()`'s
   pattern (validated destination, hardcoded event type, never
   `m.room.message`) — not a new `send/m.room.message` call.
3. Treat any CSP edit here as security-sensitive: diff against
   `apps/user/index.html` and get a second look before loosening anything.
4. If you add a new room-state field the uplink stamps (like
   `com.jkali.source`/`com.jkali.profile`/`com.jkali.mirror_of`), read it in
   `parseSnapshot()` only from `state`/`timeline` state events — never
   trust a value from the mirrored message content itself for anything
   that affects grouping, badges, or the proposal target.
