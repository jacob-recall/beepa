# Beepa — self-hosted messaging hub + manager sync

Localhost-only, self-hosted stack that pulls one person's chats from six
networks (WhatsApp, iMessage, Google Messages, Instagram, LinkedIn, X) into a
private Matrix homeserver, plus an opt-in **master-sync** layer: a teammate
can share chosen conversations, read-only, to an always-on master homeserver
where a manager reads them and can leave reply *suggestions* — never send.

**Current docs (audited 2026-08-28):**
- `docs/ARCHITECTURE.md` — what actually exists and runs, area by area
- `docs/SYSTEM-DESIGN.md` — the data schema & sharing model, plain-language
- `docs/AUDIT-FINDINGS.md` — known defects/stale spots, evidence-cited
- `docs/SIMPLIFICATION-PLAN.md` — proposed cleanups
- Per-directory `CLAUDE.md` files — deep guides (largely accurate; trust
  `docs/AUDIT-FINDINGS.md` where they disagree)
- `PLAN*.md` — historical design/decision records, one per feature wave

## Services (all loopback-only)

| Service | Where | Notes |
|---|---|---|
| **Teammate app** | `http://127.0.0.1:8011/apps/user/index.html` | the new UI: chats, share controls, contacts, proposal inbox |
| **Manager console** | `http://127.0.0.1:8011/apps/master/index.html` | read-only view across teammates; signs into the master (:8018) |
| Synapse (teammate) | `127.0.0.1:8008` | homeserver, `server_name: localhost` |
| Element Web | `http://127.0.0.1:8009` | opt-in escape hatch (media/group-admin/debug) — off by default; `docker compose --profile escape up -d element`. New chats auto-accept natively, so it is not needed day to day. |
| Synapse (master) | `127.0.0.1:8018` | compose project `matrix-master` (`master/`) |
| Enroll/admin service | `127.0.0.1:8019` | `master/enroll.py serve` (launchd) — enrollment codes, add-teammate |
| GMessages connect helper | `127.0.0.1:8020` | `gmessages-connect/connect_server.py` (launchd `com.jkali.gmessages-connect`) — one-click Google Messages login |
| IG/LI/X connect helper | `127.0.0.1:8021` | `session-connect/connect_server.py` (launchd `com.jkali.session-connect`) — one-click Instagram / LinkedIn / X login |
| iMessage daemon | `127.0.0.1:29350` | `imessage/daemon.py` (launchd appservice) |
| Uplink daemon | no port (outbound-only) | `agents/uplink/uplink.py` (launchd) — mirrors consent-shared rooms up |
| mautrix bridges ×5 | compose network only | whatsapp, meta (Instagram), linkedin, twitter, gmessages — no host ports |
| PostgreSQL ×2 | compose network only | one per stack; no host ports |

Matrix account: `@jkali:localhost` (password delivered once at setup — keep it
in your password manager).

## Setup (per machine)

```sh
./setup.sh    # brings up the stack + installs/loads the connect helpers (launchd)
```

One command, safe to re-run. It starts the local stack and turns on the
one-click Connect helpers (Google Messages + Instagram/LinkedIn/X), so no
per-login `launchctl` step is needed. Joining the manager's org is separate:
`agents/uplink/link.sh <enroll-url> <code>`.

## Daily use

```sh
docker compose --profile bridge --profile client up -d    # teammate stack
docker compose logs -f mautrix-whatsapp                   # bridge logs

# master stack (separate project — see master/CLAUDE.md):
docker compose -p matrix-master --env-file master/.env \
  -f master/docker-compose.master.yml up -d
```

Bridge commands live in each bridge's management room (or the app's
Settings tabs): `help`, `login …`, `logout`, `ping`, `sync`.

## Backfill (imported history)

Per-bridge `backfill.enabled` as configured today: WhatsApp **on**,
Google Messages **on**, LinkedIn **on**, X/Twitter **on**, Instagram
**minimal backfill** — `max_initial_messages: 20` on new portals only;
thread backfill and media (XMA) backfill are off (anti-flag posture, see
`docs/history/PLAN-META.md`); iMessage backfills via its own daemon
(`backfill_count` in `imessage/daemon.json`). Toggle in each bridge's
`config.yaml` *before* first login if you want different history behavior.

## WhatsApp login

Send `login qr` to `@whatsappbot:localhost` (or use the app's Connections
card), then phone → WhatsApp → Settings → Linked devices → Link a device →
scan. Your phone must come online at least every ~2 weeks or WhatsApp unlinks
the bridge.

## Read before deleting anything

- **`docker compose down -v` on the teammate stack is destructive after
  login**: it deletes the Postgres volume = your WhatsApp/Google Messages
  sessions *and* all bridged history. First send `logout` to each bot (or
  unlink in the phone apps), and back up if you care:
  `docker compose exec postgres pg_dumpall -U matrix > pg_dump.sql`
  (`down -v` is safe for the `matrix-master` and `matrix-synctest` projects.)
- **Stolen/lost laptop kill switch**: WhatsApp → Settings → Linked devices →
  unlink the bridge device (equivalents exist per network — see each bridge
  section below).

## Security posture (accepted trade-offs — details in PLAN.md and docs/)

- Everything binds to `127.0.0.1` only; nothing is reachable from the LAN.
- No TLS/E2BE: fine on loopback, but this stack is a **searchable plaintext
  copy of your messages** in the Postgres volume. At-rest protection is
  FileVault. Don't put this dir or Docker's data dir in unencrypted backups.
- `server_name: localhost` is permanent — a real domain later means a fresh
  homeserver.
- Unofficial clients violate each network's ToS; personal single-user
  bridging is the low-risk end, but bans are possible.
- Sharing to the master is **default-private**, four-layer consent,
  most-specific-wins; the manager is read-only by server-side power levels
  *and* by a console with no send code. Full model: `docs/SYSTEM-DESIGN.md`.
- Findings F1 and F2 from the 2026-08-28 audit were both fixed on 2026-08-28
  (legacy hub retired; `:8011` now sends hardened headers). See
  `docs/AUDIT-FINDINGS.md`.

## Updates

Images are pinned by digest, so nothing updates itself. Every month or two:
bump the image digests in `docker-compose.yml` (+ `master/docker-compose.master.yml`),
then `docker compose --profile bridge --profile client up -d`.

## Master-sync (share with your manager)

1. Manager: console → **Add teammate** → gets a one-time code (10-min TTL).
2. Teammate: app → Settings → **Connect to organization** → paste the code.
   Credentials land in the teammate's own account-data; the uplink daemon
   picks them up and starts mirroring whatever the consent rules allow
   (default: nothing).
3. Share controls: per-conversation kebab, per-contact profile, per-network,
   or the global switch — most specific wins. Un-sharing revokes the mirror.
4. Manager suggestions appear in the teammate's **Proposals** inbox; sending
   one is always the teammate's own action.

Details and guarantees: `docs/SYSTEM-DESIGN.md`; ops: `master/CLAUDE.md`,
`agents/uplink/CLAUDE.md`.

## iMessage bridge

Host-side daemon (`imessage/daemon.py`, launchd agent
`com.jkali.imessage-daemon`) bridges Messages.app ⇄ Matrix via Beeper's
platform-imessage CLI (`imessage/bin/imessage-cli`, pinned build). Chats
appear in the **iMessage** space; text works both directions; attachments are
best-effort. New chats arrive as invites.
- Install: `setup.sh` loads `imessage/com.jkali.imessage-daemon.plist` when
  `imessage/daemon.json` and `imessage/bin/imessage-cli` exist (INSTALL.md).
- Status: `launchctl print gui/501/com.jkali.imessage-daemon | grep state`
- Logs: `imessage/logs/daemon.log` (INFO is deliberately body-free)
- Restart: `launchctl kickstart -k gui/501/com.jkali.imessage-daemon`
- The local archive includes SMS/RCS fallback traffic (2FA codes etc.);
  FileVault remains the at-rest control.
- macOS permissions are granted to `imessage/bin/imessage-cli` specifically;
  a rebuilt binary re-prompts (that's a feature). Test messages
  (`pmmng-test-*`) in your self-chat are safe to delete from Messages.app.

## Google Messages bridge

Compose service `mautrix-gmessages` (digest-pinned, no host ports, internal
appservice port 29336). Bot `@gmessagesbot:localhost`; chats appear in the
**Google Messages** space; same management-room model as WhatsApp.

### Connect — one click

In the Connections card, click **Sign in & connect** on the Google Messages
card. It opens the Google sign-in tab (so your session cookies exist), then the
local connect helper (`127.0.0.1:8020`, launchd `com.jkali.gmessages-connect`)
reads your Google session from Chrome (approve the one-time macOS Keychain
prompt) and submits it to the bridge. The only manual step left is **tapping the
emoji** it shows, in the Google Messages app on your phone.

- The helper is loopback-only and reads cookies **only** when you click the
  button; a page from any other origin cannot drive it (Origin +
  `X-Beepa-Connect` gate). See `gmessages-connect/CLAUDE.md`.
- **CLI fallback** (if the helper is not running): `python3
  gmessages-connect/connect.py` — same flow from a terminal.
- **Your phone must stay continuously online** — Google Messages proxies
  every message through the phone.
- **Re-linking:** click **Sign in & connect** again; `logout` (Settings)
  clears the login.

## Instagram bridge (mautrix-meta)

Compose service `mautrix-meta` (digest-pinned, no host ports, own DB
`mautrix_meta`). Bot `@instagrambot:localhost`; chats appear in the
**Instagram** space. Design + security dispositions:
`docs/history/PLAN-META.md`. Minimal backfill only (see Backfill above).

### Connect — one click (no terminal/paste)

In the app's Connections card, click **Connect** on the Instagram card. The
local connect helper (`127.0.0.1:8021`, launchd `com.jkali.session-connect`)
reads your `instagram.com` session cookies from Chrome (any signed-in
profile; approve the one-time macOS Keychain prompt) and submits them to the
bridge's provisioning API directly — no DevTools, no paste, no message that
needs deleting. See `session-connect/CLAUDE.md`.

- **CLI equivalent**: `python3 session-connect/connect.py instagram`.
- **Fallback if the button fails** (stale/incomplete session, etc.):
  1. In your everyday Chrome (2FA enabled), log into instagram.com normally.
  2. DevTools → Network → filter XHR → type `graphql`; click around so a
     `graphql` request appears.
  3. Right-click it → **Copy → Copy as cURL**.
  4. DM `@instagrambot:localhost`, send `login instagram`, paste the cURL
     when prompted (or the cookies `sessionid, csrftoken, mid, ig_did,
     ds_user_id` as JSON).
  5. **Immediately delete that message** — it is a bearer credential. The
     bridge log is `info`-level and does not record it.

### Staying un-flagged
Home IP only (never a VPS), 2FA on, minimal backfill, one account. This is a
unified Meta bridge with no `network.mode` field: only ever run
`login instagram` — `login facebook`/`messenger` would bridge those too.

### Kill switch
Instagram → Accounts Center → Password & security → Where you're logged in →
remove the bridge device (or reset password), then `logout` to the bot.

## LinkedIn bridge (mautrix-linkedin)

Compose service `mautrix-linkedin` (digest-pinned, no host ports, own DB
`mautrix_linkedin`). Bot `@linkedinbot:localhost`; **LinkedIn** space.

### Connect — one click (no terminal/paste)

In the app's Connections card, click **Connect** on the LinkedIn card. The
local connect helper (`127.0.0.1:8021`) reads your `linkedin.com` session
cookies from Chrome (incl. the httpOnly `li_at`), synthesizes the two
LinkedIn tracking headers (`X-LI-Track` / `X-LI-Page-Instance`) no cookie
store holds, and submits everything to the bridge's provisioning API
directly. See `session-connect/CLAUDE.md`.

- **CLI equivalent**: `python3 session-connect/connect.py linkedin`.
- **Fallback if the button fails** (LinkedIn ever rejects the synthesized
  headers, etc.), session-paste like Instagram:
  1. Log into linkedin.com normally in your everyday Chrome.
  2. DevTools → Network → filter `graphql` (voyager API); click around.
  3. Right-click a request → **Copy → Copy as cURL**.
  4. App → Connections → LinkedIn → paste the cURL, **Submit session** —
     sent through the management-room guard and **auto-redacted**
     immediately. (Or DM the bot `login cookies` manually and delete the
     pasted message yourself.)

LinkedIn needs the full cURL (not just cookies) in the fallback path: the
`X-LI-Track` / `X-LI-Page-Instance` headers ride alongside the `li_at`
cookie. Never paste a real cURL anywhere else — it is a bearer credential.
If auto-redact ever fails, the card offers an in-app "Delete it now" retry;
only if that also fails bring up the opt-in Element escape hatch (`docker
compose --profile escape up -d element`) to delete the message there. ToS
caveat and `down -v` warning as per WhatsApp. Commands: `help`, `version`,
`login cookies`, `list-logins`, `logout <id>`, `set-preferred-login`,
`search`, `start-chat`, `resolve-identifier`, `sync`.

## X (Twitter) bridge (mautrix-twitter)

Compose service `mautrix-twitter` (digest-pinned, no host ports). Bot
`@twitterbot:localhost`; chats appear in the **X** space. Cookie/session-paste
login like Instagram/LinkedIn, driven from the app's Connections card (the
paste is sent through the management-room guard and auto-redacted). Same ToS,
home-IP, and `down -v` cautions as the other session-paste bridges.
