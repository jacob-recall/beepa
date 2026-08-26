# Local WhatsApp↔Matrix bridge

Self-hosted [mautrix-whatsapp](https://github.com/mautrix/whatsapp) stack,
localhost-only, on Docker Compose. See `PLAN.md` for the full design, security
review, and dispositions.

## Services
| Service | Where | Notes |
|---|---|---|
| **Bridge Hub** | `http://127.0.0.1:8010` | **start here** — connect bridges, settings buttons, QR login |
| Synapse | `127.0.0.1:8008` | homeserver, `server_name: localhost` |
| Element Web | `http://127.0.0.1:8009` | your chats live here |
| mautrix-whatsapp | compose network only | bridge bot: `@whatsappbot:localhost` |
| mautrix-linkedin | compose network only | bridge bot: `@linkedinbot:localhost` |
| PostgreSQL 16 | compose network only | DBs: `synapse`, `mautrix_whatsapp`, `mautrix_linkedin` |

## Bridge Hub
`hub/` is a static page (no backend) that signs into Synapse as you and drives
the bridge bot: Connections view (per-bridge card, Connect shows the QR
inline, Disconnect), WhatsApp settings view (every management command as an
explained button + output console). Add future bridges to the `BRIDGES` array
in `hub/site/app.js`. Design + security review: `PLAN-HUB.md`. Backfill is
**enabled** (recent history: ~50 msgs/chat over the last ~3 months of chats,
imported on link).

Matrix account: `@jkali:localhost` (password delivered once at setup — keep it
in your password manager).

## Daily use
```sh
docker compose --profile bridge --profile client up -d    # start everything
docker compose --profile bridge --profile client down     # stop (keeps data)
docker compose logs -f mautrix-whatsapp                   # bridge logs
```
Open http://127.0.0.1:8009 → sign in as `jkali` → the room with
`@whatsappbot:localhost` is the bridge management room.

Bridge commands (in the management room): `help`, `login qr`, `logout`,
`ping`, `sync`.

## WhatsApp login
Send `login qr` to the bot, then on your phone: WhatsApp → Settings →
Linked devices → Link a device → scan the QR the bot posts. Chats appear as
rooms within ~a minute. Your phone must come online at least every ~2 weeks or
WhatsApp unlinks the bridge.

History backfill is **off** (privacy default): old messages are not imported;
new messages bridge from login onward. To import history, set
`backfill.enabled: true` in `whatsapp/config.yaml` (limits are alongside it)
and `docker compose restart mautrix-whatsapp` — do this *before* logging in if
you want initial history.

## Read before deleting anything
- **`docker compose down -v` is destructive after WhatsApp (or Google
  Messages) login**: it deletes the Postgres volume = your WhatsApp/Google
  Messages sessions *and* all bridged history for both. First send `logout`
  to each bot (or unlink in WhatsApp → Linked devices / Google Messages →
  Device pairing), and take a backup if you care:
  `docker compose exec postgres pg_dumpall -U matrix > pg_dump.sql`
- **Stolen/lost laptop kill switch**: WhatsApp → Settings → Linked devices →
  unlink the bridge device. That immediately invalidates the session keys
  stored on this machine.

## Security posture (accepted trade-offs — details in PLAN.md)
- Everything binds to `127.0.0.1` only; nothing is reachable from the LAN.
- No TLS/E2BE: fine on loopback, but this stack is a **searchable plaintext
  copy of your WhatsApp messages** in the Postgres volume. At-rest protection
  is FileVault (verified on). Don't put this dir or Docker's data dir in
  unencrypted cloud backups.
- `server_name: localhost` is permanent — migrating to a real domain later
  means starting over with a fresh homeserver.
- Using an unofficial WhatsApp client violates WhatsApp ToS; personal
  single-user bridging is the low-risk end, but a ban, while unlikely, is
  possible.

## Updates
Images are pinned by digest (supply-chain integrity), so nothing updates
itself. Every month or two: bump the four image tags/digests in
`docker-compose.yml` (Synapse for security fixes; mautrix-whatsapp *will*
eventually break against WhatsApp protocol changes if left stale), then
`docker compose --profile bridge --profile client up -d`.

## iMessage bridge (Phase 1)
Host-side daemon (`imessage/daemon.py`, launchd agent
`com.jkali.imessage-daemon`) bridges Messages.app ⇄ Matrix via Beeper's
platform-imessage CLI (`imessage/bin/imessage-cli`, pinned build). Chats
appear in the **iMessage** space; text works both directions; attachments
are best-effort. New chats arrive as invites (accept in Element).
- Status: `launchctl print gui/501/com.jkali.imessage-daemon | grep state`
- Logs: `imessage/logs/daemon.log` (INFO is deliberately body-free)
- Restart: `launchctl kickstart -k gui/501/com.jkali.imessage-daemon`
- The archive note from the WhatsApp section now also covers iMessage —
  including SMS/RCS fallback traffic (2FA codes etc.), so the local archive's
  sensitivity went up. FileVault remains the at-rest control.
- macOS permissions are granted to `imessage/bin/imessage-cli` specifically;
  a rebuilt binary re-prompts (that's a feature). Test messages
  (`pmmng-test-*`) in your self-chat are safe to delete from Messages.app.

## Google Messages bridge
Compose service `mautrix-gmessages` (digest-pinned, no host ports, internal
appservice port 29336). Bot `@gmessagesbot:localhost`; chats appear in the
**Google Messages** space. Uses the same management-room model as WhatsApp: a
2-member DM with the bot, driven from the Hub.

### Connect — 3 steps
Google Messages links with a quick **Google account sign-in** (no cookie pasting;
credentials never become a Matrix message). From the Hub → **Connections → Google
Messages** card, or from a terminal:

1. **Open Google sign-in** (the card's button, or
   `https://accounts.google.com/AccountChooser?continue=https://messages.google.com/web/config`)
   and sign into your Google account in Chrome.
2. **Run the connect helper:** `python3 gmessages-connect/connect.py`
   (approve the one-time macOS Keychain prompt). It reads your Google session
   from Chrome and submits it to the bridge.
3. **Tap the emoji** it prints, in the Google Messages app on your phone.

The card then shows **Connected**; your ~25 recent chats + contacts sync into the
**Google Messages** space (new chats arrive as Element invites, like WhatsApp).
Backfill is **on**. Commands (list-logins, logout, sync, start-chat, …) live in
the Hub's **Settings → Google Messages** tab.

- **Your phone must stay continuously online** — Google Messages proxies every
  message through the phone; if it goes offline, messages pause until it's back.
- **Re-linking / testing:** just run `gmessages-connect/connect.py` again.
  `logout` (Hub Settings) or the provisioning `logout` endpoint clears the login;
  "Delete all bridged rooms" clears the synced rooms.

## Instagram bridge (mautrix-meta)
Compose service `mautrix-meta` (Instagram DM bridge, digest-pinned, no host
ports, own DB `mautrix_meta`). Bot `@instagrambot:localhost`; chats appear in
the **Instagram** space. Design + security dispositions: `PLAN-META.md`.
Backfill is **off** — only messages from login onward are bridged.

### Log in (you do this — no automation, on purpose)
Automated input on Instagram's login page is the classic bot-detection trigger,
so **you** log in as a human and hand the bridge the resulting session:
1. In your **everyday Chrome** (the one Instagram already trusts), with **2FA
   enabled** on your account, go to instagram.com and log in normally.
2. Open DevTools → **Network** tab → filter **XHR** → type `graphql` in the
   filter. Click around Instagram so a `graphql` request appears.
3. Right-click a `graphql` request → **Copy → Copy as cURL** (POSIX on Windows).
4. In Element, open a DM with `@instagrambot:localhost`, send `login instagram`,
   and paste the cURL when prompted. (Alternatively paste the cookies
   `sessionid, csrftoken, mid, ig_did, ds_user_id` as a JSON object.)
5. **Immediately delete that message.** The cURL/cookies are a *bearer
   credential* (anyone with `sessionid` is you): unless deleted, it rests in the
   Matrix events DB. Element: hover the message → **Remove** (redact). The
   bridge log is deliberately `info`-level and does **not** record the cookie.

Chats appear as rooms in the Instagram space within a minute or two.

### Staying un-flagged (single personal account)
- Runs on **localhost / your home IP** — do **not** move this to a VPS/datacenter
  IP (the single biggest flag for a bridged Instagram session).
- **2FA on**; backfill off; the bridge only syncs your DM inbox participants
  (no follower/following scrape). Keep it to your one account.
- This is a unified Meta bridge: this image has **no `network.mode` field** —
  the network is chosen when you run `login instagram`. Only run
  `login instagram`; `login facebook`/`login messenger` would bridge those too.

### Kill switch (if the laptop is lost, or you want to cut the session)
Instagram → Settings → **Accounts Center → Password & security → Where you're
logged in** → remove the bridge device (or reset your password). That
invalidates the `sessionid` stored locally. Then `logout` to the bot.

### Commands (DM the bot)
`help`, `version`, `login instagram`, `list-logins`, `logout <login ID>`.

### Don't destroy your session
Same warning as WhatsApp: after login, **`docker compose down -v` deletes the
Instagram session + bridged history**. Send `logout` to the bot (or remove the
device in Instagram) and `pg_dump` first if you care.

## LinkedIn bridge (mautrix-linkedin)
Compose service `mautrix-linkedin` (digest-pinned, no host ports, own DB
`mautrix_linkedin`). Bot `@linkedinbot:localhost`; chats appear in the
**LinkedIn** space. Like Instagram, this is a **session-paste** login: you log
in as a human on linkedin.com and hand the bridge the resulting session — there
is no automated login on LinkedIn's page (that's the classic bot-detection
trigger).

### Log in (you do this — no automation, on purpose)
1. In your everyday Chrome, go to linkedin.com and log in normally.
2. Open DevTools → **Network** tab → type `graphql` in the filter (LinkedIn's
   web app calls its **voyager** graphql API). Click around LinkedIn so a
   `graphql`/voyager request appears.
3. Right-click a `graphql` request → **Copy → Copy as cURL**.
4. From the Hub → **Connections → LinkedIn** card, click **Connect LinkedIn**
   (this sends `login cookies` to the bot and opens linkedin.com), then **paste
   the cURL** into the box and **Submit session**. The Hub sends it to the bot
   through the management-room guard and **auto-redacts** the pasted message
   immediately. (Alternatively, DM `@linkedinbot:localhost` and send
   `login cookies`, then paste the cURL yourself and delete that message.)

Unlike a cookies-only bridge, LinkedIn needs the **cURL** (not just cookies):
the request carries the `X-LI-Track` and `X-LI-Page-Instance` headers that
LinkedIn's voyager API expects alongside the `li_at` cookie. A trimmed,
fake-valued example of what you'd paste (real values redacted):

```
curl 'https://www.linkedin.com/voyager/api/...' \
  -H 'csrf-token: <REDACTED>' \
  -H 'x-li-track: {"clientVersion":"<REDACTED>"}' \
  -H 'x-li-page-instance: urn:li:page:<REDACTED>' \
  -b 'li_at=<REDACTED>; JSESSIONID=<REDACTED>'
```

Never paste a real cURL into a file, a ticket, or a chat log — it is a **bearer
credential** (anyone with `li_at` is you).

### At-rest / redaction note
The cURL is a bearer credential. The Hub sends it **only** to the LinkedIn
management room and redacts the carrier event right away; the bridge log is
deliberately `info`-level and does **not** record the cookie. Until redacted,
a pasted credential rests in the Matrix events DB — if the auto-redact ever
fails, delete the message in Element (hover → **Remove**). At-rest protection
for the local archive is FileVault (verified on).

### ToS caveat
Using an unofficial LinkedIn client violates LinkedIn's Terms of Service.
Personal single-user bridging is the low-risk end, but a restriction or ban,
while unlikely, is possible. Runs on **localhost / your home IP** — don't move
this to a VPS/datacenter IP. Keep it to your one account.

### Commands (DM the bot)
`help`, `version`, `login cookies`, `list-logins`, `logout <login ID>`,
`set-preferred-login`, `search`, `start-chat`, `resolve-identifier`, `sync`.

### Don't destroy your session
Same warning as WhatsApp: after login, **`docker compose down -v` deletes the
LinkedIn session + bridged history**. Send `logout` to the bot and `pg_dump`
first if you care.
