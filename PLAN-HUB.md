# Plan: Bridge Hub + backfill re-link (bridge-hub-v2)

User-approved design: a static, localhost-only web page ("Bridge Hub") that
drives the mautrix-whatsapp bot through the Matrix client API as `@jkali`,
plus: enable recent-history backfill, and clear the existing WhatsApp login
(user's explicit request) so the next link imports history + contacts.
v2 folds in the security review (H-1…H-17, dispositions below).

## Outcome
- `whatsapp/config.yaml`: `backfill.enabled: true` (limits untouched: 50
  initial msgs/chat, 500 catch-up; history sync covers ~3 months, one room per
  conversation in that window); bridge restarted.
- Existing WhatsApp login `14146149941` logged out and all old portal rooms
  deleted (`delete-all-portals`), after a precautionary `pg_dump`.
- New compose service **hub**: digest-pinned `nginx:alpine` (profile `client`)
  on `127.0.0.1:8010`, serving `hub/` read-only.
- Hub page (no backend, no build step): sign-in view, Connections view
  (config-driven bridge cards; WhatsApp card with live status, Connect→QR
  inline, Disconnect), WhatsApp Settings view (command buttons with
  one-sentence explanations + plain-text output console), link to Element.

## Canonical origin (H-7)
`http://127.0.0.1:8010` everywhere — page fetch URLs target
`http://127.0.0.1:8008`, CSP names `http://127.0.0.1:8008`, docs/bookmarks say
`127.0.0.1`, never `localhost`.

## Command surface (from live `help` output, v26.08)
Buttons (one-sentence explanation each):
- General: `help`, `version`.
- Account: `list-logins`, `login qr`, `login phone`, `relogin <id>`,
  `logout <id>` (confirm), `set-preferred-login <id>`, and `cancel` as a
  flow-scoped "Cancel login" button rendered only while a login flow is
  active (H3 implements it; it terminates a pending flow before user
  hand-off).
- Chats & contacts: `start-chat <identifier>`, `search <query>`,
  `resolve-identifier <identifier>`, `join <invite link>`,
  `resolve-link <link>`, `sync groups`, `sync contacts`.
- Relay: `set-relay` (confirm), `unset-relay`.
- Advanced: `debug-reset-network` (confirm).
- Danger zone: `delete-all-portals` (type-to-confirm the word `delete`).
Excluded — portal-scoped, belong in Element: sync-portal, id, unbridge,
delete-chat, mute, set-pl, invite-link, accept, bridge, create-portal,
create-group, filter, import-image-pack, doin, sudo, debug-account-data,
debug-register-push, set-management-room. Excluded — H-2: `login-matrix` (invites pasting a raw
access token into a plaintext room; double puppeting unconfigured), and with
it `logout-matrix`/`ping-matrix` (useless without it).

## Security constraints (dispositions H-1…H-17 folded in)
- **Headers (H-1, H-5, H-13)** — nginx serves, with `add_header ... always`
  in one location block only:
  `Content-Security-Policy: default-src 'self'; connect-src 'self'
  http://127.0.0.1:8008; img-src 'self' blob:; style-src 'self';
  script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none';
  frame-ancestors 'none'; require-trusted-types-for 'script';
  trusted-types 'none'`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Permissions-Policy: camera=(), microphone=(), geolocation=()`,
  `Cross-Origin-Opener-Policy: same-origin`, `Cache-Control: no-store`.
- **Rendering (H-5, H-15)**: no `innerHTML`/`insertAdjacentHTML`/`outerHTML`
  anywhere (Trusted Types enforces); all remote strings via `textContent`
  after stripping C0 controls and bidi overrides (U+202A–202E, U+2066–2069,
  zero-width chars), length-clamped, `white-space: pre-wrap`, console visually
  contained so remote text can't imitate hub chrome. QR events render as image
  only — their `body`/`formatted_body` (which contains the raw code) is never
  shown. Edits (`m.replace`) accepted only when `edit.sender === botMxid ===
  original.sender`. The QR image is cleared (and its blob URL revoked) on
  whichever arrives first: an `m.room.redaction` of the QR event, or the
  bot's reply to `cancel` (sender-checked, same management room).
- **Media (H-4)**: QR fetched via authenticated
  `/_matrix/client/v1/media/download/{server}/{id}` only after validating
  `^mxc://([A-Za-z0-9.\-:]+)/([A-Za-z0-9_-]+)$`, requiring server part
  `localhost`, `encodeURIComponent` on both segments; Blob type hardcoded
  `image/png`; blob URLs revoked on replace/redact, never `window.open`ed.
- **Management-room targeting (H-6)**: resolve the room with the bot once,
  verify before EVERY destructive send (bot joined, exactly two members:
  `@jkali:localhost` + bot, not a portal); refuse to send on mismatch.
- **Token/session (H-8, H-16)**: access token in `sessionStorage` (not
  localStorage); `device_id` alone may persist in localStorage for reuse;
  `initial_device_display_name: "Bridge Hub"`; password/token never logged to
  console or displayed; 401/`M_UNKNOWN_TOKEN` clears storage → sign-in view
  (no retry loop); Sign out calls `/logout` server-side then clears.
- **Action safety (H-9)**: destructive buttons confirm (delete-all-portals is
  type-to-confirm); all buttons disabled while a command is in flight; fresh
  random txnId per user action.
- **nginx/container (H-12)**: `server_tokens off`, `autoindex off`, GET/HEAD
  only, own config mounted at `/etc/nginx/conf.d/default.conf:ro`,
  `read_only: true` + tmpfs for `/var/cache/nginx` `/var/run`,
  `security_opt: [no-new-privileges:true]`. Hub static files are 644 with
  `hub/` 755 (they contain no secrets — H-12; project root stays 700, so
  host-side exposure is unchanged). `[::]` inside the container is fine; the
  acceptance check is about HOST listeners.
- Everything else unchanged from the base stack: provisioning disabled, no
  docker socket, no new secrets, no third-party origins.
- **Test credential hygiene (H-3)**: the automated browser pass never types
  the user's password and never screenshots/dumps a live QR; it (a) tests the
  sign-in form's failure path with a deliberately wrong password, (b) tests
  authenticated flows by injecting the existing setup token into
  sessionStorage, (c) asserts the QR structurally only (an `img` with `blob:`
  src and `naturalWidth > 0`). The setup token is invalidated (device logout)
  at the end.

## Slices
- **H1 — Backfill on**: `pg_dump` first (also serves H2); set
  `backfill.enabled: true`; restart bridge only.
  *Acceptance*: config enabled; clean bridge startup; bot answers
  `list-logins`.
- **H2 — Clear connection** (user-requested, irreversible):
  `logout 14146149941` then `delete-all-portals` (that order — H-10).
  *Acceptance*: `list-logins` shows no logins; `@jkali`'s joined rooms contain
  no WhatsApp portals (management room and possibly an empty personal-space
  room remain — noted); report notes media_store is not purged.
- **H3 — Hub service**: `hub/{index.html,app.js,style.css,nginx.conf}` +
  compose service; start.
  *Acceptance*: 200 on `http://127.0.0.1:8010/`; ALL headers above present;
  host listeners: exactly 127.0.0.1:{8008,8009,8010}, no `0.0.0.0`/`[::]`;
  compose `ports:` literal `127.0.0.1:` prefix; hub `image:` contains an
  `@sha256:` digest and the running container's image matches it; no
  `innerHTML` &c. in app.js (grep); no inline handlers/scripts in index.html.
> **H4 executed deviation (user-approved 2026-08-25):** the Chrome extension
> was not connected, so H4 ran as API-level checks mirroring the page's exact
> call paths (wrong-password → 403 M_FORBIDDEN; mgmt-room resolution via
> joined_members; `login qr` → bot m.image event; mxc validated by the page's
> regex, local server; authenticated media fetch returned a real PNG; `cancel`
> → QR-clear signal within 30s, observed via the bot's cancel-reply — the
> no-redaction case the dual-trigger design covers; app.js parses clean under
> node 22). DOM rendering itself is covered by a 2-minute user checklist in
> the final report instead of automated browser assertions.

- **H4 — Functional pass (Chrome automation, per H-3 hygiene)**: wrong
  password → visible error; token-injection → Connections card renders status
  "Not connected"; Settings `version` button → bot reply appears in console
  panel; Connect → structural QR assert; click "Cancel login" → bot confirms
  cancellation and the QR disappears from the UI within a bounded wait (30s),
  removed on either the cancel-confirmation reply or an `m.room.redaction`
  (redaction-on-cancel is unverified upstream, so the UI reacts to either
  signal; this also guarantees no pending login flow is left live before the
  user's real scan). No screenshots of the QR; page JS console free of
  errors (checked via pattern excluding expected 401).
- **H5 — Verification**: fresh `pilotfish:verifier` on the exact claim
  (hub headers+origin, listener set, sign-in failure path, bot round trip via
  page path, backfill enabled, logins empty, portals gone, provisioning still
  disabled, no secrets in hub files, hub files contain no `innerHTML` sinks).
  Setup token/device revoked after.

## Rollback
- H1: set `backfill.enabled: false`, restart bridge.
- H2: not reversible by me — re-linking is the user's QR scan (intended next
  step); `pg_dump` from H1 preserves pre-deletion state if ever needed.
- H3: `docker compose rm -sf hub`, delete `hub/`, revert compose entry.

## Stops
Any slice failing after 2 distinct fix attempts → stop and report. Never scan
or capture the WhatsApp QR; never send commands beyond the listed surface.

## Security review dispositions (pilotfish:security-reviewer, 2026-08-25)
No P0/P1. | H-1 clickjacking → mitigated (frame-ancestors/XFO/base-uri/
form-action) | H-2 login-matrix token paste → mitigated (button omitted) |
H-3 test credential handling → mitigated (no password typing, structural QR
assert, token invalidated) | H-4 mxc injection → mitigated (validate+encode,
local server only, fixed blob type) | H-5 innerHTML/edit/redaction traps →
mitigated (Trusted Types, sender checks, redaction handling) | H-6 room
mis-targeting → mitigated (management-room verification per destructive send)
| H-7 origin split → mitigated (canonical 127.0.0.1) | H-8 password origin
squat → accepted under F6 + device name + sessionStorage | H-9 confirmation
design → mitigated (type-to-confirm, in-flight disable, fresh txnIds) | H-10
H2 sequencing → accepted (order kept; pg_dump first; space-room + media_store
residue noted) | H-11 backfill scope → accepted within F10, stated in report |
H-12 container hardening → mitigated (free bits; 644 statics deviation
recorded) | H-13 headers → mitigated | H-14 Synapse CORS unchanged → accepted
(no reverse proxy in front of /_matrix, ever) | H-15 display spoofing →
mitigated (sanitize+clamp+contain) | H-16 session edges → mitigated (401
handling, device_id reuse) | H-17 full-power token → accepted (Matrix has no
scoped tokens; CSP/TT are the load-bearing controls).

## Amendment A1 (2026-08-25, rev 2): embed the Hub inside Element
User request: hub should not be a separate screen. Element cannot host custom
settings pages; the integrated equivalent is a widget in the bridge
management room (iframe, scripts allowed), standalone :8010 kept working.
Security review A1-1..A1-11 completed; corrections folded in below.

Changes:
1. hub/nginx.conf: CSP `frame-ancestors 'none'` -> `frame-ancestors 'self'
   http://127.0.0.1:8009`; REMOVE `X-Frame-Options` (cannot express an
   allowlist and would override the CSP); Permissions-Policy extended with
   `display-capture=(), clipboard-read=()` (A1-10).
2. Element frame protection (A1-1/A1-2/A1-3): mount a modified copy of the
   image's own `/etc/nginx/templates/default.conf.template` at that same
   template path (`:ro`), preserving `${ELEMENT_WEB_PORT}`, both listens, the
   `/config` (root /tmp/element-web-config), `/i18n/`, `/version`, `/modules`
   locations verbatim, and adding EXACTLY
   `Content-Security-Policy: frame-ancestors 'self'` +
   `X-Frame-Options: SAMEORIGIN` (never 'none'/DENY — Element frames itself
   for file downloads; CSP header contains ONLY frame-ancestors) — added at
   server level AND repeated inside every location block that declares its
   own add_header (`= /index.html`, `= /version`, `/i18n/`, `/config`),
   because nginx add_header does not merge.
3. element/config.json: `UIFeature.widgets: true`.
4. Pre-checks (A1-5, A1-7): @jkali power level in the mgmt room allows
   `im.vector.modular.widgets` state events; `account_data/m.widgets` 404s.
5. Widget event, sent AS @jkali (A1-4/A1-6): type `im.vector.modular.widgets`,
   state_key `bridge-hub`, content
   `{"type":"custom","url":"http://127.0.0.1:8010","name":"Bridge Hub"}`.

Accepted risks: A1-6 bot-rewrite of the widget triggers Element consent
prompt (control, not risk); A1-7 user-widgets prompt bypass is
post-compromise only; A1-8 popup path is spoofing-only (per-tab
sessionStorage shows sign-in, not an authed hub); A1-9 port-squat framing of
the hub by a local process is within the trusted set; A1-11 canonical origin
stays 127.0.0.1 (widget loads only when Element is opened at
http://127.0.0.1:8009; frame-ancestors is NOT widened to localhost).

Acceptance:
(a) `curl -sI` on Element `/`, `/index.html`, `/version`, and the config URL
    Element fetches (`/config.json`): each 200 with BOTH
    `Content-Security-Policy: frame-ancestors 'self'` and
    `X-Frame-Options: SAMEORIGIN`; config.json body contains
    `"UIFeature.widgets": true`.
(b) hub `/`: 200, CSP shows `frame-ancestors 'self' http://127.0.0.1:8009`,
    NO X-Frame-Options header, Permissions-Policy includes display-capture
    and clipboard-read; standalone hub unaffected (app.js served, sign-in
    reachable).
(c) mgmt room state contains the `im.vector.modular.widgets`/`bridge-hub`
    event with sender @jkali:localhost and the exact content above.
(d) User-observed (same deviation class as H4, Chrome automation
    unavailable): opening Element at http://127.0.0.1:8009, the mgmt room
    shows the Bridge Hub widget rendering the hub UI with no consent prompt
    and no frame-ancestors violation in the browser console. API-level
    proxies for (d) are (a)+(b)+(c).

Rollback: restore `frame-ancestors 'none'` + `X-Frame-Options: DENY` in
hub/nginx.conf and restart hub; remove the element template mount from
docker-compose.yml and recreate the element container; set
`UIFeature.widgets: false`; neutralize the widget by sending
`im.vector.modular.widgets` state_key `bridge-hub` with empty content `{}`
as @jkali (state events cannot be deleted, only overwritten).

Stops: any acceptance item failing after 2 distinct fix attempts -> rollback
and report.

### Standing permissions invariant (2026-08-25, supersedes per-file sweeps)
Synapse creates media files at runtime with default modes, so "no file
group/other-readable" is not a stable property of `synapse/media_store/`.
The enforced invariant is: (a) all secret-bearing files 600 (homeserver.yaml,
signing key, both registration/config yamls, .env, pg_dump_pre_relink.sql); (b) ancestor dirs
`pm_mng/`, `synapse/`, `synapse/media_store/` at 700 so runtime-created 644
media is never reachable by other users; (c) the intentional 644/755 web
assets (hub/, element/nginx-default.conf.template) contain no secrets.
Verified by two verifier passes; future audits check (a)-(c), not raw mode
bits under media_store.

## Amendment A2 (2026-08-25): Hub becomes the single UI shell
User feedback on A1: widget embed is not "one UI" (widget not discoverable;
connections not reachable from Element). New architecture: the Hub at
http://127.0.0.1:8010 is the ONLY entry point, with three tabs:
- **Chats** (default after sign-in): Element embedded in an iframe
  (http://127.0.0.1:8009). Same-site (ports excluded from site), so Element's
  own session storage is NOT partitioned — an existing Element login just
  works inside the frame.
- **Connections**: existing bridge cards, plus a "More sources" card listing
  planned bridge types (Telegram, Signal, etc.) as inert placeholders — the
  BRIDGES array is the extension point; a new bridge = new array entry once
  its bot exists on the homeserver.
- **Settings**: the per-bridge command surface (structured per-bridge even
  though only WhatsApp exists today).

Framing direction inverts; A1's widget approach is retired:
1. hub/nginx.conf CSP: `frame-ancestors` back to `'none'`, restore
   `X-Frame-Options: DENY` (hub is framed by nobody again); ADD
   `frame-src 'self' http://127.0.0.1:8009` (default-src would otherwise
   block the Chats iframe).
2. element/nginx-default.conf.template: `frame-ancestors 'self'
   http://127.0.0.1:8010` and REMOVE X-Frame-Options entirely (XFO cannot
   express an allowlist and SAMEORIGIN would block the hub; CSP governs in
   all supported browsers — same reasoning as A1 change 1). Headers still
   repeated in every location block that sets its own add_header.
3. element/config.json: `UIFeature.widgets` back to `false` (A1's enable was
   for the widget only).
4. Retire the A1 widget: overwrite `im.vector.modular.widgets`/`bridge-hub`
   with empty content `{}` as @jkali (state events cannot be deleted).
5. hub/site: add Chats tab + iframe; default post-sign-in view = Chats;
   "More sources" placeholder card in Connections; Settings unchanged in
   function. No new JS sinks; iframe src is a hardcoded constant, never
   derived from data.

Security deltas vs A1 (for review): Element is now framable by exactly one
origin (8010) while the hub returns to unframeable; evil.com still cannot
frame either (needs to be an allowed ancestor). Element-in-iframe
considerations: nested same-origin usercontent/ download iframe inside the
cross-origin hub frame; notifications/popups degraded inside the frame
(direct 8009 access remains available and supported); no widget surface
remains (UIFeature.widgets false).

Acceptance:
(a) hub `/`: CSP has `frame-ancestors 'none'` AND `frame-src 'self'
    http://127.0.0.1:8009`; `X-Frame-Options: DENY` present.
(b) Element `/`, `/index.html`, `/version`, `/config.json`: 200, CSP
    `frame-ancestors 'self' http://127.0.0.1:8010`, NO X-Frame-Options;
    config.json body has `"UIFeature.widgets": false`.
(c) mgmt-room state `im.vector.modular.widgets`/`bridge-hub` has empty
    content and sender @jkali.
(d) hub index.html contains the three-tab nav and an iframe whose src is
    exactly `http://127.0.0.1:8009`; app.js has no new HTML-string sinks
    (grep) and iframe src is a constant.
(e) User-observed (same deviation class as H4/A1): signing into the hub
    shows the Chats tab with Element rendering inside it; Connections and
    Settings tabs work; no CSP violations in console. API proxies: (a)-(d).

Rollback: restore A1-state hub/nginx.conf (frame-ancestors 'self' 8009, no
XFO, no frame-src) and element template (frame-ancestors 'self' + XFO
SAMEORIGIN), re-set UIFeature.widgets true, re-send the A1 widget content,
revert hub/site tab changes.

Stops: any acceptance failing after 2 distinct fix attempts -> rollback,
report.

### A2 security review dispositions (rev 2 deltas — final spec)
No P0/P1; architecture confirmed sound (framed app holds no hub token; hub
returns to unframeable). Corrections folded in:
- A2-1: hub `Cross-Origin-Opener-Policy` becomes `same-origin-allow-popups`
  (plain `same-origin` severs window.open from the framed Element; incoming
  direction unchanged — evil.com still gets a severed handle).
- A2-7: `frame-src http://127.0.0.1:8009` exactly (no `'self'`).
- A2-4: iframe pinned — created only AFTER hub sign-in, torn down with
  `remove()` (never display:none) on sign-out; NO `sandbox` attribute;
  `allow="clipboard-write; fullscreen"`.
- A2-3: button labeled "Sign out of Hub"; sign-out removes the iframe and
  shows a note that the Element session at http://127.0.0.1:8009 is separate.
- A2-2 (accept): file downloads inside a cross-origin frame are broken
  upstream (element-web#22951); the "Open chats in a full tab →" link to
  http://127.0.0.1:8009 stays as the documented fallback. Acceptance (b)
  additionally checks `/usercontent/index.html` serves the frame headers.
- A2-5: kill the localhost/127.0.0.1 split — README chat links become
  127.0.0.1:8009; element/config.json base_url (and the
  enable_presence_by_hs_url key) become http://127.0.0.1:8008. First Chats
  use may require one Element sign-in inside the frame if the prior session
  was on the localhost origin; after that, never again.
- A2-6 (accept, recorded): a local process squatting 8009 while Element is
  down would render inside the hub's trusted chrome — mirror of A1-9, same
  trusted-local-process boundary.
- A2-9: also clear the A1 widget-consent residue: mgmt-room account data
  `im.vector.setting.allowed_widgets` set to `{}`.
- A2-10 (hygiene rider): `pg_dump_pre_relink.sql` added to the 600-mode
  secret-file list in the standing invariant.
- Acceptance (e) softened per A2-5: Chats tab renders Element; an
  already-signed-in Element is expected when the prior session used the
  127.0.0.1 origin, otherwise a one-time sign-in inside the frame is
  accepted and noted.

### A2 FINAL consolidated acceptance & rollback (supersedes all earlier A2 acceptance/rollback text)
Acceptance:
(a) hub `/`: 200; CSP contains `frame-ancestors 'none'` AND
    `frame-src http://127.0.0.1:8009` (no `'self'` token in frame-src);
    `X-Frame-Options: DENY`;
    `Cross-Origin-Opener-Policy: same-origin-allow-popups` (this supersedes
    the `same-origin` value in the H-1/H-13 header list above — the canonical
    COOP for the hub is now same-origin-allow-popups).
(b) Element `/`, `/index.html`, `/version`, `/config.json`,
    `/usercontent/index.html`: each 200 with CSP
    `frame-ancestors 'self' http://127.0.0.1:8010` and NO X-Frame-Options.
    `/config.json` body: `"UIFeature.widgets": false`,
    `"base_url": "http://127.0.0.1:8008"`, `enable_presence_by_hs_url` keyed
    on `http://127.0.0.1:8008`, and zero occurrences of `localhost:8008`.
(c) mgmt-room state `im.vector.modular.widgets`/`bridge-hub` content is `{}`
    (sender @jkali), and mgmt-room account data
    `im.vector.setting.allowed_widgets` reads `{}`.
(d) hub index.html has the three-tab nav (Chats/Connections/Settings) and NO
    static iframe; app.js creates the iframe only after sign-in with src
    constant `http://127.0.0.1:8009`, no `sandbox` attribute,
    `allow="clipboard-write; fullscreen"`, and `remove()`s it on sign-out;
    sign-out button reads "Sign out of Hub"; the "Open chats in a full tab"
    link to http://127.0.0.1:8009 is present; no HTML-string sinks (grep).
(e) User-observed (H4/A1 deviation class): hub sign-in → Chats tab renders
    Element inside the shell; already-signed-in Element expected if the prior
    session used the 127.0.0.1 origin, else a one-time in-frame sign-in is
    accepted; Connections and Settings tabs work; no CSP violations.
(f) README chat links say 127.0.0.1:8009 (no localhost:8009 remains);
    `pg_dump_pre_relink.sql` is mode 600 and named in the standing invariant.

Rollback (complete inverse of every A2 mutation):
- hub/nginx.conf: frame-ancestors → `'self' http://127.0.0.1:8009`, remove
  frame-src, remove X-Frame-Options, COOP → `same-origin`; restart hub.
- element/nginx-default.conf.template: frame-ancestors → `'self'`, re-add
  `X-Frame-Options: SAMEORIGIN` in server + all four location blocks;
  recreate element container.
- element/config.json: `UIFeature.widgets` → true; `base_url` and
  `enable_presence_by_hs_url` key → `http://localhost:8008`; recreate/reload.
- README: chat links → previous values.
- Re-send A1 widget content as @jkali; `allowed_widgets` stays `{}`, so
  Element will re-prompt consent once (accepted).
- hub/site: revert tab/iframe/sign-out changes.
