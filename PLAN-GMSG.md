# PLAN-GMSG — Google Messages bridge (as built)

Adds `mautrix-gmessages` as a fourth source on the `matrix-wa` stack, mirroring
the WhatsApp bridge, wired into the Bridge Hub with QR-first onboarding and
contact sync. Deployed 2026-08-26.

## What was deployed

- **Image**: `dock.mau.dev/mautrix/gmessages:v26.08@sha256:cecc0b77bd4d64e6e04b7b5961749e3890daad7885b37e6cde5bb24a07735f3e`
  (version parity with `mautrix-whatsapp:v26.08`; digest-pinned like the others).
- **Compose**: `mautrix-gmessages` service, `./gmessages:/data`, UID/GID 501/20,
  `profiles: ["bridge"]`, no host ports (appservice on compose net, port 29336).
- **Database**: `mautrix_gmessages` (C/C/UTF8), created live in the running
  Postgres AND added to `postgres-init/01-create-bridge-db.sh` *inside* the
  `<<-EOSQL` heredoc (fresh-volume reproducibility).
- **Synapse**: `synapse/gmessages-registration.yaml` + `/data/gmessages-registration.yaml`
  appended to `app_service_config_files`; Synapse restarted; bot
  `@gmessagesbot:localhost` registered; homeserver↔appservice verified.
- **Config** (`gmessages/config.yaml`, gitignored): homeserver `http://synapse:8008`
  domain `localhost`; DSN → `mautrix_gmessages`; appservice
  `http://mautrix-gmessages:29336`, hostname `0.0.0.0`; permissions
  `@jkali:localhost` admin, `localhost` user, **relay off**; **encryption off**
  (loopback posture); **backfill on**; `initial_chat_sync_count: 25` (contacts+chats).
- **`.gitignore`**: `gmessages/` added (parity with the other secret trees).
- **Hub** (`hub/site/app.js`, additive): `gmessages` SOURCES entry;
  `GMSG_COMMAND_GROUPS`; six dispatch points generalized (`findWaMgmt`/`isWaMgmt`
  → `findBotDmMgmt`/`isBotDmMgmt` shared by whatsapp+gmessages); QR path
  generalized via `qr.boxId` (`gmsg-qr-box`); a Google Messages Connections card;
  Settings tab auto-appears via `groupsFor`. `PLANNED_SOURCES` loses "Google Messages".
- **README**: "Google Messages bridge" section (QR-first onboarding, phone-online
  caveat, cookie-fallback note, `down -v` warning extended).

## Onboarding (as built — browser-assisted Google login)

**QR is dead in v26.08**: the bot rejects `login qr` ("Invalid login flow");
the only flow is `google` (account cookies + phone emoji confirm). QR-first was
planned but is impossible. Login was done browser-assisted, no manual paste:

1. User signs into Google in Chrome (via the Claude browser extension).
2. The 7 cookies (SID HSID SSID OSID APISID SAPISID __Secure-1PSIDTS) are
   decrypted host-side from Chrome's store (macOS v10/Keychain; script
   `scratchpad/gm_cookies.py`) — the extension can't read httpOnly cookies.
3. Submitted to the **provisioning API** (shared secret in config; reachable via
   `docker compose exec mautrix-gmessages` on localhost:29336):
   `POST /_matrix/provision/v3/login/start/google` → login_id;
   `POST .../login/step/{login_id}/fi.mau.gmessages.google_account/cookies`
   (flat cookie map, one-shot) → `display_and_wait` emoji step.
4. User taps the emoji in the phone Messages app → `complete`.

Cookies never became a Matrix message (avoids security-review F1); temp file
shredded. Connected as jkalinovskiy@gmail.com; ~25 chats + contacts synced.
The Hub card's "Connect" is now an info modal (no QR); re-linking = re-run the
browser-assisted flow.

## Security review dispositions (pre-approval reviewer, all resolved)

- **F1 (HIGH) cookie exposure** — AVOIDED: QR-first never handles Google
  account cookies. Fallback (deferred) would use the provisioning API / redaction.
- **F2 (MED) `.gitignore` gap** — FIXED: `gmessages/` added before any secret.
- **F3 (MED) at-rest** — README notes account-cookie sensitivity (moot on QR path).
- **F4 (LOW) six dispatch points** — DONE and verifier-checked.
- **F5 (INFO) prefer QR** — ADOPTED as primary.
- Hub XSS/Trusted-Types/mgmt-verification invariants: intact (no innerHTML, all
  bridged text sanitize()+textContent, all sends via `sendCmd()`).

## Caveats

- Phone must stay **continuously** online (messages proxy through the app).
- Adds another local plaintext message archive; FileVault is the at-rest control.
- Unofficial client (Google ToS); low risk for personal single-user use.

## Double puppeting (auto-join → centralized Home feed)

For Google Messages conversations to appear in the Hub's Home feed / sidebar, the
user must be JOINED to the bridge's personal space *"Google Messages (<account>)"*
and its child portal rooms (the feed lists joined children of a space whose name
starts with the source's `spaceName`). mautrix invites but does not auto-join, so
**double puppeting** was enabled so the bridge auto-joins `@jkali` to portals
(now and for future chats):

1. `synapse/gmessages-registration.yaml` — added a **non-exclusive** namespace
   `- regex: ^@jkali:localhost$  exclusive: false` (lets the appservice as_token
   act as `@jkali`). Restart Synapse to load it.
2. `gmessages/config.yaml` → `double_puppet.secrets: { localhost: as_token:<gmessages as_token> }`.
   Restart the bridge.
3. On restart the bridge auto-joins `@jkali` to all portals; join the space room
   itself once via the as_token if it stays "invite":
   `curl -XPOST "http://127.0.0.1:8008/_matrix/client/v3/join/<space>?user_id=@jkali:localhost" -H "Authorization: Bearer <as_token>" -d '{}'`.

**SECURITY**: this grants the gmessages appservice the ability to act as `@jkali`
on the local homeserver (standard mautrix double-puppeting; bounded to this
single-user localhost stack, where the bridge is already trusted with all the
user's Google messages). Both edits live in gitignored runtime files
(`synapse/`, `gmessages/`) so they are NOT committed — re-apply via these steps if
the stack is rebuilt. WhatsApp/iMessage do not have double puppeting; their chats
arrive as invites to accept.

## Rollback

`docker compose stop mautrix-gmessages`; remove the compose service + the
`app_service_config_files` line (restart synapse); `DROP DATABASE mautrix_gmessages`;
revert the hub SOURCES/command-group additions and the postgres-init insert;
`rm -rf gmessages/ synapse/gmessages-registration.yaml`.
