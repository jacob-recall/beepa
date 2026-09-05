# session-connect/ — the one-click Instagram / LinkedIn / X login helper

Turns Instagram/LinkedIn/X connect into one click in `apps/user`: the
teammate clicks **Connect** on the network's card and the bridge is logged
in, with no DevTools, no Copy-as-cURL, no paste. A browser cannot read
Chrome's cookie store or `docker compose exec` the bridge, so a tiny
loopback service (`127.0.0.1:8021`, launchd `com.jkali.session-connect`)
does it on the browser's behalf — the exact shape and security posture of
`gmessages-connect/`, extended to three networks plus an extra interactive
step for X and a read-only number-enrichment endpoint.

## What lives here

- `connect.py` — the provisioning logic, **usable standalone as a CLI**
  (`python3 session-connect/connect.py {twitter|linkedin|instagram}`). Its
  functions are imported by the server without running `main()`:
  - `NETWORKS` — per-network config: bridge service/port, `config.yaml`
    path, provisioning `flow` name, and the cookie `domain`/`signal` used
    to pick the right Chrome profile.
  - `shared_secret()` — reads the bridge provisioning secret from the
    network's `config.yaml`.
  - `resolve_fields()` — reads Chrome cookies via `chrome_cookies.read()`
    and, for fields the bridge asks for that live in a request header
    rather than a cookie, calls `synth_header()`.
  - `synth_header()` — rebuilds the `Cookie` header from the jar (including
    httpOnly cookies) and synthesizes LinkedIn's two tracking headers
    (`X-LI-Track`, `X-LI-Page-Instance`) that no cookie store holds — the
    bridge only pattern-checks them, LinkedIn's read APIs accept a
    well-formed value.
  - `api()` — calls the bridge provisioning API inside its container via
    `docker compose exec` (fixed argv list, `shell=False`).
  - `provisioning_login()` — drives one network's login end to end,
    including the `user_input` loop (used by X's XChat passcode step); the
    CLI path prompts with `getpass` (never echoed).
  - `ID_RE` — F2: `^[A-Za-z0-9._-]+\Z` for validating any bridge-returned
    `login_id`/`step_id` before it is interpolated into a provisioning-API
    path.
- `chrome_cookies.py` — shared cookie reader (same approach as
  `gmessages-connect/connect.py`): copies each Chrome profile's cookie DB to
  a private 0600 `mkstemp` file, derives the AES key from the "Chrome Safe
  Storage" Keychain item, decrypts the v10 blobs, and deletes the temp copy
  in a `try/finally`. **Multi-profile**: Chrome keeps one cookie store per
  profile (`Default`, `Profile 1`, …); `read()` scans every profile for the
  target domain and picks the one that actually holds the logged-in session
  — the profile whose jar carries the network's `signal` cookie (e.g.
  LinkedIn's `li_at`) wins, falling back to the profile with the most
  matching cookies. Never merges across profiles, so a returned jar is
  always one profile's session and can't mix two accounts.
- `connect_server.py` — the loopback helper (mirrors
  `gmessages-connect/connect_server.py` and `master/enroll.py serve`):
  single-threaded `HTTPServer` on `127.0.0.1` (port 8021, or the first
  free port in 8021-8025 if held), silent
  access log, CORS preflight locked to the app's own origin.
- `run-connect.sh` + `com.jkali.session-connect.plist` — the launchd
  service (`RunAtLoad`, `KeepAlive`, `Umask 63` = 0o77). `run-connect.sh`
  puts docker on `PATH` because `api()` shells `docker compose exec`. Logs
  go to `logs/` (gitignored).

## The three login flows

All three networks are completed **server-side** via the bridge's
provisioning API: the session is read, submitted, and discarded inside this
process; nothing but a generic status (+ the linked account localpart) is
ever returned (F6) — the credential never touches a Matrix room.

- **twitter** — `flow: "cookies"`. Reads `x.com` cookies (`ct0`,
  `auth_token`); submits directly.
- **linkedin** — `flow: "cookies"`. Reads `linkedin.com` cookies (incl. the
  httpOnly `li_at`) plus the two synthesized tracking headers; submits
  directly. If LinkedIn ever rejects the synthesized headers, the app's
  paste box (Copy-as-cURL) is the documented fallback.
- **instagram** — `flow: "instagram"`, the mautrix-meta `ig-` build's
  dedicated login flow. Same shape as the other two under the hood (reads
  `sessionid`/`csrftoken`/`ds_user_id`/`mid` cookies from the `instagram.com`
  jar and submits them), just addressed at a different provisioning
  endpoint name.
- **X's extra step** — after cookies, X's bridge can respond
  `type: user_input` (the XChat passcode). The CLI loops with `getpass`;
  the server surfaces it as `POST /connect/<net>/start` →
  `{status: input_required, login_id, step_id, fields}`, the Hub renders
  the field(s), and `POST /connect/<net>/input` submits the value back here
  — the browser never sees the session, only supplies this one short-lived
  value, which is never logged or returned (F6).

## Endpoints

- `GET  /connect/health` → `200 "ok"` — **pure liveness, zero side effects**
  (no cookie read, no Keychain, no bridge call, no CORS headers).
- `POST /connect/<twitter|linkedin|instagram>/start` → reads cookies, starts
  the bridge login, submits them, and returns one of:
  `{status: complete, account}` | `{status: input_required, login_id,
  step_id, instructions, fields}` | `{status: failed}`.
- `POST /connect/<net>/input` → `{login_id, step_id, values}` in, submits
  the interactive step (e.g. X's passcode) to the bridge, same
  complete/input_required/failed shape back.
- `POST /enrich/numbers` → `{numbers: {room_id: {value, kind, source}}}` —
  calls the read-only `agents/enrich/number_resolver.resolve_all()` (a pure
  `SELECT` across the bridge databases, no writes anywhere on this path)
  and returns each 1:1 conversation's real phone number/email. Like the
  cookie returns, the values go only to the authorized loopback origin and
  are never logged.
- `POST /contacts/list` → `{contacts: [{source, network_id, kind,
  display_name}]}` — the teammate's OWN imported address book
  (`agents/contacts/contacts.db`), opened **read-only** (`sqlite mode=ro`, so
  this process can never create or migrate it), soft-deleted rows excluded,
  rows filtered to the known source ids (`CONTACTS_SOURCES`, kept in step with
  the uplink's `SOURCE_ID_TO_LABEL`), in keyset pages of `CONTACTS_MAX` (2000), returning `next_cursor`.
  The authorized JSON request body may supply that cursor; the UI reads every
  page and shows a failure if any later page cannot be loaded. `apps/user` uses it to offer a
  per-contact share override for contacts that have no conversation (e.g. an
  alerting bot), which no conversation row could otherwise reach.
  - **Deliberately a POST, not a GET (F1).** This helper's GETs are ungated
    liveness-only by design; a GET here would be unauthenticated, and adding
    one would also widen the shared `Access-Control-Allow-Methods` header for
    every other endpoint.
  - **Fixed path.** Pagination is in the JSON body, never a query string, so the
    one diagnostic line (`_diag`, which logs `self.path`) cannot leak a handle.
  - **Host allowlist (F1b).** In addition to `_authorized()`, the `Host` header
    must be `127.0.0.1:<bound port>` or `localhost:<bound port>` — DNS-rebinding
    defence-in-depth, since a rebound name resolves to loopback but arrives
    carrying the attacker's `Host`.
  - **F6 posture**, identical to `/enrich/numbers`: the values are real phone
    numbers / emails / display names; they go only to the authorized loopback
    origin and are **never logged**.
  - **ACCEPTED RESIDUAL (local process, documented per the per-contact-share
    plan's F1 disposition).** Any process running as this user that can speak
    the loopback protocol — correct `Origin`, `Content-Type`,
    `X-Beepa-Connect`, and now `Host` — can read the imported address book.
    That is a *broader* read than `/enrich/numbers` (which returns only the
    handles of conversations that already exist). It is accepted for the same
    reason as the residuals below: a local process running as this user can
    already read `contacts.db` (mode 600) directly, so the endpoint grants no
    capability the attacker does not already have. Do **not** widen it into a
    GET, a query-parameterized search, or an unfiltered dump of other stores.
- `POST /enroll/exchange` → `{master_url, code}` in; the server-side leg of
  the app's **Connect to organization** flow. The browser cannot fetch a
  remote master origin (`apps/user`'s CSP `connect-src` is loopback-only), so
  the helper POSTs `{code}` to `<master_url>/enroll/exchange` on its behalf and
  returns ONLY the five credential fields the master hands back
  (`master_hs_url, master_user, master_token, manager_mxid, master_space` —
  `ENROLL_FIELDS`), which the app then writes to its own local account-data
  (`com.jkali.master_link`). SSRF containment: `https` only, or `http` only to
  a loopback master; no redirects (`_NoRedirect` — a 3xx could carry the code
  elsewhere); bounded timeout (`ENROLL_TIMEOUT`) and a tight response cap
  (`ENROLL_MAX_RESP`, since only 5 tiny fields come back). The one-time code and
  the returned scoped credentials are NEVER logged or echoed (F6). Gate-tested by
  `tests/unit/enroll_proxy_guard.test.py`.
  - **Reviewed residuals (all LOW, all gated behind an attacker who already
    controls the app origin):** (1) this is by design a confused-deputy proxy —
    it faithfully relays to whatever `master_url` the human pasted, so a rogue
    master URL means the teammate's *shared* conversations mirror to that master;
    the trust decision is the human, not the helper (the app frames it as "paste
    what your manager sends you"). (2) `ENROLL_TIMEOUT` is a per-socket, not a
    total, deadline — the response cap bounds it but a hostile upstream the user
    was tricked into could still slow-drip. Both are inherent to "connect to the
    master you're told" and accepted for the loopback-only, tailnet-scoped
    internal threat model; do not add a direct-remote-fetch path that would move
    the exchange out from behind this F1 gate.

## Security invariants (from `connect_server.py`'s docstring — do not weaken)

- **F1 — every `do_POST` is gated by `_authorized()` before any side
  effect**: (a) `Origin` ∈ the two loopback aliases of the user's own app
  (`http://127.0.0.1:8011`, `http://localhost:8011`); (b) `Content-Type ==
  application/json`; (c) `X-Beepa-Connect: 1`. (b)+(c) are non-simple
  headers, forcing a cross-origin page into a CORS preflight that fails,
  since the server only ever echoes the one allowed origin.
- **Loopback only.** Binds `127.0.0.1` — never `0.0.0.0` or `""`. The port is
  8021, falling back to the first free port in **8021–8025** if the default is
  held by a foreign process (common on managed Macs), and the chosen loopback
  base is published to `apps/user/connect.local.json` so the app knows which
  port to fetch (the CSP whitelists the whole range). The **host** (127.0.0.1)
  is the security boundary, not the specific port.
- **F5 — `GET /connect/health` has zero side effects and no CORS.** No path
  reads cookies, the Keychain, or the bridge at import, on `start`, on a
  timer, or from health — only an authorized `POST` does.
- **F6 — the provisioning `shared_secret`, cookies, passcodes, and raw
  bridge response bodies are never returned or logged.** Failures map to
  fixed generic messages (`log_message` is a no-op; the one diagnostic
  line, `_diag`, carries only method/path/origin/status). `/enrich/numbers`
  follows the same posture: real phone numbers/emails go only to the
  authorized origin, never to a log. `/enroll/exchange` likewise: the
  one-time enrollment code, the `master_url`, and the returned scoped
  master credentials go only to the authorized origin and are never logged;
  only the five `ENROLL_FIELDS` are relayed, never the raw upstream body.
- **F2 — bridge-returned `login_id`/`step_id` are validated** with
  `connect.ID_RE` before being interpolated into a provisioning-API path,
  on both `/start` and `/input`.
- **F1b — `/contacts/list` additionally pins the `Host` header** to this
  listener's own loopback name:port (`_host_allowed`, using the port `serve()`
  actually bound). It runs *after* `_authorized()`, never instead of it.

## How to run / test

```bash
# Normal path: ./setup.sh installs + loads this (and the Google Messages
# helper) for you.
# Manual equivalent:
cp session-connect/com.jkali.session-connect.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.jkali.session-connect.plist 2>/dev/null
launchctl load  ~/Library/LaunchAgents/com.jkali.session-connect.plist

# liveness (side-effect-free — no Keychain prompt):
curl -s http://127.0.0.1:8021/connect/health          # -> ok

# guard checks (these never reach a cookie read, so no Keychain prompt):
curl -si -X POST http://127.0.0.1:8021/connect/instagram/start                     # 403 (no Origin)
curl -si -X POST http://127.0.0.1:8021/connect/instagram/start -H 'Origin: http://evil.example'  # 403

# CLI equivalent for any of the three, from a terminal:
python3 session-connect/connect.py {twitter|linkedin|instagram}
```

Do **not** trigger a fully-authorized `/start` in an unattended test: it
reads the user's Chrome cookies, fires a Keychain prompt, and starts a real
login against the live bridge. Verify the guards + health only; test the
happy path live.

Host services now use generated `org.beepa.*` launchd jobs and the managed
Python runtime. Legacy plist files are compatibility templates, not files to
copy directly. Request bodies have bounded length and a total read deadline;
rejected or incomplete bodies never start bridge login or cookie capture.
