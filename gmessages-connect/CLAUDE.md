# gmessages-connect/ — the one-click Google Messages login helper

Turns Google Messages connect into one click in `apps/user`: the teammate
clicks **Sign in & connect** on the Google Messages card and the only manual
step is tapping an emoji in the Google Messages app on their phone. A browser
cannot read Chrome's cookie store or `docker compose exec` the bridge, so a
tiny loopback service does it on the browser's behalf.

## What lives here

- `connect.py` — the provisioning logic, **usable standalone as a CLI**
  (`python3 gmessages-connect/connect.py`). Its functions are imported by the
  server without running `main()`:
  - `shared_secret()` — reads the bridge provisioning secret from
    `gmessages/config.yaml`.
  - `decrypt_cookies()` — copies Chrome's cookie DB to a **private 0600
    `mkstemp` file** (F3), reads the Keychain "Chrome Safe Storage" key,
    decrypts the Google session cookies, and deletes the temp copy in a
    `try/finally` so a crash never orphans a readable copy.
  - `api()` — calls the bridge provisioning API via `docker compose exec`.
  - `valid_login_id(s)` — F2: `True` only for `^[A-Za-z0-9_-]+$`; the server
    validates any bridge-returned `login_id` with this before interpolating it
    into a provisioning-API path.
- `connect_server.py` — the loopback helper (mirrors `master/enroll.py serve`):
  single-threaded `HTTPServer` on **exactly `127.0.0.1:8020`**, silent access
  log, CORS preflight locked to one origin.
- `run-connect.sh` + `com.jkali.gmessages-connect.plist` — the launchd service
  (`RunAtLoad`, `KeepAlive`, `Umask 63` = 0o77). `run-connect.sh` puts docker on
  `PATH` because `api()` shells `docker compose exec`. Logs go to `logs/`
  (gitignored).

## Endpoints

- `GET  /connect/health` → `200 "ok"` — **pure liveness, zero side effects**
  (no cookie read, no Keychain, no bridge call, no CORS headers).
- `POST /connect/gmessages/start` → `{"emoji": "<x>"}` — decrypt cookies, start
  the bridge login, submit cookies, return the tap-emoji. Stores the bridge
  `login_id` **server-side only**.
- `POST /connect/gmessages/wait` → `{"status":"complete","account":"<localpart>"}`
  | `{"status":"timeout"}` | `{"status":"failed"}` — waits (≤ ~2 min) for the
  emoji tap. Takes **no body params** (F2): it uses only the stored `login_id`
  (`409` if none in progress). Clears the stored id on any terminal result.

## Security invariants (do not weaken)

- **Loopback only.** Binds `127.0.0.1:8020`, never `0.0.0.0` / `""`. Nothing
  off this machine can reach it.
- **F1 — the authorization gate runs at the TOP of every `do_POST`, before any
  side effect** (before a single cookie is read or any bridge call is made).
  All three are required, fail-closed:
  1. `Origin` request header == `http://127.0.0.1:8011` exactly — a missing,
     `"null"`, or different Origin is refused `403`. This is the primary gate.
  2. `Content-Type` == `application/json`.
  3. `X-Beepa-Connect: 1` header present.
  (2)+(3) are non-simple headers, so a cross-origin page is forced into a CORS
  preflight, which fails because the server only ever echoes the one allowed
  origin. `do_OPTIONS` answers the preflight for that origin only, never `"*"`.
- **No auto-connect.** No path decrypts cookies, reads the Keychain, or starts
  a login at import, at process start, on a timer, or from health — ONLY an
  authorized `POST /start` does (F5/F7).
- **Health is side-effect-free** (F5): liveness only, and carries no CORS.
- **Cookies, the `shared_secret`, and raw bridge bodies are never returned or
  logged** (F6). `log_message` is a no-op; bridge/cookie failures map to fixed
  generic messages ("Google session not found — sign into Google in Chrome and
  try again." for the cookie step; "Could not start Google Messages login."
  otherwise). The only values that leave the process are the tap-emoji and the
  account localpart (before `/`).
- **Server-held `login_id`** (F2): validated with `valid_login_id()` before any
  path interpolation, held in a module global, never sent to or accepted from
  the client. `/wait` cannot be pointed at another login by the browser.
- **Single in-flight login** (single-user machine): a second `/start` overwrites
  the stored `login_id`; the previous in-progress login is abandoned.

## Known tradeoff

Approving the macOS Keychain prompt as **"Always Allow"** grants the
`security`/`python3` binary standing read access to the "Chrome Safe Storage"
key — a machine-wide grant, not scoped to this helper. That is the same grant
the CLI has always required; the one-click flow does not widen it, but be aware
that any local process able to run `security find-generic-password` after an
"Always Allow" can also derive the cookie-decryption key. Prefer "Allow" (per
prompt) if you want to keep it one-shot.

## How to run / test

```bash
# install + load the launchd service:
cp gmessages-connect/com.jkali.gmessages-connect.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.jkali.gmessages-connect.plist 2>/dev/null
launchctl load  ~/Library/LaunchAgents/com.jkali.gmessages-connect.plist

# liveness (side-effect-free — no Keychain prompt):
curl -s http://127.0.0.1:8020/connect/health          # -> ok

# guard checks (these never reach decrypt_cookies, so no Keychain prompt):
curl -si -X POST http://127.0.0.1:8020/connect/gmessages/start                     # 403 (no Origin)
curl -si -X POST http://127.0.0.1:8020/connect/gmessages/start -H 'Origin: http://evil.example'  # 403
```

Do **not** trigger a fully-authorized `/start` in an unattended test: it reads
the user's Chrome cookies, fires a Keychain prompt, and starts a real login
needing the phone. Verify the guards + health only; test the happy path live.
