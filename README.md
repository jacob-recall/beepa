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
| PostgreSQL 16 | compose network only | DBs: `synapse`, `mautrix_whatsapp` |

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
- **`docker compose down -v` is destructive after WhatsApp login**: it deletes
  the Postgres volume = your WhatsApp session *and* all bridged history.
  First send `logout` to the bot (or unlink in WhatsApp → Linked devices),
  and take a backup if you care:
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
