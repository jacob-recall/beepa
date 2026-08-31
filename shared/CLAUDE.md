# shared/ — the single source of truth

Native ES modules (no bundler, no build step) imported directly by
`apps/user/` and `apps/master/`. Design/behavior changes made here are
inherited by both apps; the "few exceptions" live in the app dirs, not here.
See PLAN-MASTER-SYNC.md §10 and PLAN-MASTER-SYNC-IMPL.md Phase 1/5.

## What lives here

- `state.js` — the single mutable-state object `S` (token, userId, sync
  cursors, caches) plus a few exported `Map`/`Set` collections. ES modules
  can't share a reassignable top-level `let` across files, so every
  reassigned field lives as a property on `S`; the always-mutated-in-place
  collections (`convosBySource`, `feedModel`, …) stay as plain exported
  `const`s. `apps/master/main.js` keeps its own separate `MS` object for
  master-only state rather than overloading `S`.
- `matrix/client.js` — the transport: `api()` (fetch wrapper, bearer auth,
  401 → `onUnauthorized` hook), `ROOMID_RE`/`MXC_RE` validation regexes, and
  `configureMatrixBase()` so a page can repoint the transport at a different
  homeserver (used only by `apps/master`). Each HTML page gets its own ES
  module graph, so repointing one page's copy never affects another.
- `model/consent.js` — the authorization boundary (PLAN §4): the pure
  4-level resolver `resolve()`/`effectiveShared()`/`resolveAll()`
  (per-conversation override > contact-profile share > per-source policy >
  global policy > safe default `private`), normalization helpers, and
  account-data storage helpers for `com.jkali.share_policy` /
  `com.jkali.share_override`. **Must stay byte-parity with
  `agents/uplink/consent.py`** — same precedence, same reason strings, same
  normalization. `tests/unit/consent.test.js` and
  `tests/unit/consent_py.test.py` assert the two independently; if you change
  one, change the other and re-run both.
- `model/contacts.js` — the unified-contacts model (PLAN §12 phase 5):
  `ContactProfile { id, displayName, roomIds, share }`, stored in
  `com.jkali.contact_profiles`. Enforces "a room belongs to at most one
  profile" and "share ∈ {share,private,inherit}" in `normalizeProfiles()` —
  never trust stored data past that function. Linking/unlinking/sharing is
  manual only; `suggestions()` is pure and advisory (never mutates, never
  auto-merges).

  **Approved exception — `autoMergeByNumber()`.** This is the ONE sanctioned
  auto-merge path: it groups conversations that a trusted local helper has
  resolved to the same real phone number (E.164), so the same person on two
  bridges lands in one profile without manual curation. It is safe because
  it (1) only ever CREATES an auto-profile (`share: 'inherit'`, never
  `'share'` — so auto-merge never shares anything new with the manager) or
  ADDS rooms to a single already-existing profile without touching that
  profile's share; and (2) NEVER merges two existing profiles — if a
  number's rooms already span two or more distinct profiles it skips the
  number entirely, so a deliberate user separation is never undone. It is
  pure and idempotent.
- `ui/el.js` — `$`, `el()` (textContent-only DOM builder), `sanitize()` /
  `sanitizeLine()` (strip control/bidi/zero-width chars, clamp length),
  `txn()`. Every other module's only DOM-safety primitive.
- `ui/render.js` — **the render whitelist + the from_me anti-spoof gate.**
  `convoResolveContent()` reads `content.body` only, never `formatted_body`;
  media types get a static label, never a filename. `renderMessageEvent()` is
  the single shared bubble renderer for both history and live events,
  dedups by `event_id`/`transaction_id`, and decides "sent" (right-aligned)
  from `ev.sender === S.userId`, OR a *trusted* `com.jkali.from_me` flag —
  trusted **only** when `ev.sender === IMSG_BOT_MXID` — OR `S.selfMxids`
  (cosmetic only, never relaxes the from_me gate). See "Security invariants"
  below before touching this file.
- `ui/chat.js` — `openConvo()` (validated room open + history load + live
  watch), `sendConvoMessage()` — **the only external send path** (see
  `apps/user/CLAUDE.md`) — and the room-scoped `/sync` tail.
- `ui/rows.js`, `ui/search.js`, `ui/nav.js`, `ui/sources.js`,
  `ui/account-data.js`, `ui/connections.js` — convo-row rendering, Home
  feed / directory search, section navigation, the per-bridge `SOURCES`
  table + command console, account-data-backed feed sync, and the
  connections/settings cards. Several expose an **app-injection hook**
  pattern (`setConvoRowDecorator`, `setSourceViewHook`, `setSharingViewHook`,
  `setContactsViewHook`, `setProposalsViewHook`, `setOnUnauthorized`) so an
  app can extend shared UI without `shared/` ever importing from `apps/`.
  Keep that direction one-way: never add an `import` from `shared/` into
  `apps/user/` or `apps/master/`.

## Security invariants (do not weaken)

- **CSP + Trusted Types** live in each app's `index.html`, not here, but
  every module in `ui/` was written to satisfy `require-trusted-types-for
  'script'` — i.e. it never assigns to `innerHTML`/`outerHTML`/etc. and
  only uses `el()`/`textContent`. A new module must keep that discipline.
- **No `innerHTML` anywhere.** All text — including sanitized message
  bodies — goes into its own `el()` node via `textContent`.
- **The render whitelist is the anti-XSS/anti-phishing boundary.**
  `convoResolveContent()` must keep reading `content.body` only. Never add a
  path that reads `formatted_body` or renders a filename for media.
- **The from_me anti-spoof gate.** A remote party must never be able to
  render as "You". The `com.jkali.from_me` flag is honored only when
  `ev.sender === IMSG_BOT_MXID` (imported from `ui/sources.js`). If you add
  a new bridge/bot whose daemon also stamps a from_me-style flag, gate it
  the same way — sender identity, not a content field, is what's trusted.
- **A management room is a 2-member bot DM that is NOT a portal and NOT a
  space.** `ui/sources.js`'s `isBotDmMgmt()` is what `findBotDmMgmt()` /
  `verifyMgmt()` trust before `sendCmd()` and — critically —
  `sendSecretToMgmt()`, which posts a **session credential** into whatever it
  returns. Membership alone does not identify that room: a degenerate portal
  (every ghost gone) and a bridge's own SOURCE SPACE (e.g. "WhatsApp (+1…)")
  are both 2-member `{bot, user}` rooms. So the predicate additionally fetches
  the room's full state and refuses anything carrying `uk.half-shot.bridge`
  (mautrix's portal marker) or an `m.room.create` with `type: 'm.space'` —
  the same absence-of-a-portal test `verifyImsgMgmt()` already made. If the
  state cannot be read, the room is refused (a room we cannot prove is not a
  portal is not a management room). Never relax this back to a membership-only
  check, especially now that `apps/user` auto-joins bridge invites and so has
  more candidate rooms.
- **Room-id / mxc validation before concatenation.** `ROOMID_RE` /
  `MXC_RE` (from `matrix/client.js`) must validate any id before it is
  concatenated into a URL path. `consent.js`'s `overridePath()` and
  `contacts.js`'s room-id normalization both do this — keep doing it in any
  new storage helper.
- **JS ↔ Python consent parity is a hard requirement, and the conformance
  harness is its authority.** `model/consent.js` and
  `agents/uplink/consent.py` must resolve identically on every input: same
  precedence order, same reason strings (`'explicit'`, `'excluded'`,
  `'all <source>'`, `'profile: <name>'`, `'private'`), same normalization,
  and the SAME input canonicalisation (the shared type gates: plain-object /
  non-empty-string / `SOURCE_KEY_RE` own-property lookups /
  `CONSENT_ROOMID_RE` — see the table in
  `docs/superpowers/plans/2026-08-30-consent-conformance.md`). Parity is
  proven on every test run by `tests/conformance/consent_conformance.py`
  (~84k exhaustive + fuzz vectors through BOTH real modules; any differing
  output or crash on either side is a red build, and a `DECISION DIFFERS`
  class is the leak class). A change to one file without the matching change
  to the other is a shipped authorization bug — the uplink (Python) is what
  actually decides whether a conversation leaves the machine. Two boundary
  rules: `effectiveShared()`/`effective_shared()` is the only value the
  uplink acts on, and `reason` strings are UI-only — never parse them for
  authorization.

## How to run / test

```bash
# consent resolver unit tests (JS side), via the pinned node:20-alpine:
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/consent.test.js
# tests/run.sh runs all 19 unit tests, including the python-side mirror
# (consent_py.test.py) and the uplink reconcile test — see tests/CLAUDE.md
tests/run.sh

# a quick syntax check across every shared module:
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine sh -c \
  'for f in shared/**/*.js shared/*.js; do node --check "$f" || exit 1; done'
```

There is no standalone dev server for `shared/` — it is only exercised
through `apps/user/index.html` / `apps/master/index.html` (open directly, or
via the running Docker stacks in `docker-compose.yml` / `master/`).

## How to change this safely

1. A change here affects **both** apps — check `apps/master/main.js`'s file
   header before touching `ui/render.js`, `ui/rows.js`, `ui/nav.js`,
   `ui/search.js`, `ui/sources.js`, `ui/chat.js`, or `ui/connections.js`: the
   master app *deliberately* does not import that whole graph (importing
   any one of them would pull in `sendConvoMessage`/`sendCmd` transitively,
   since ES module imports execute eagerly), and re-implements the small
   read-only subset it needs locally instead. If you refactor one of those
   files, re-check that master's local re-implementation (in
   `apps/master/main.js`) still matches the new shape, or accept that it
   is now a stale duplicate needing a matching edit.
2. Never import from `apps/` here. New app-specific behavior gets a new
   `set<X>Hook()` callback, following the existing pattern in `rows.js` /
   `search.js` / `nav.js`.
3. Touching `consent.js` or `contacts.js` is security-sensitive (the
   authorization boundary / the sharing unit): update
   `agents/uplink/consent.py` in lockstep for any resolver change, and run
   both `tests/unit/consent.test.js` and `tests/unit/consent_py.test.py`
   before considering the change done.
4. Touching `render.js` is security-sensitive (anti-spoof gate, render
   whitelist): a diff of `convoResolveContent()` and the `trustedFromMe`
   condition is part of any review.
5. No bundler, no transpilation — every file must stay a valid native ES
   module runnable directly by a browser (`node --check` is a good enough
   syntax smoke test in dev).
