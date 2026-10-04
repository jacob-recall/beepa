# Discord DMs and group DMs

Beepa uses the unmodified mautrix-discord 0.7.7 image, pinned to manifest
`sha256:065405ca2f961b2687ca577c4eb65592c139d641342a9611d98b5394f30cf84a`
(AMD64 and ARM64). The matching upstream tag points to
`c62165a46109d7c824bc0b4bb067ba02ea6528f1`. The bridge is AGPL-3.0;
[upstream source and license](https://github.com/mautrix/discord/tree/v0.7.7)
remain available there. No bridge fork is included.

## Connect

Install with `setup.sh`, or apply a reviewed committed release with the normal
updater. Both paths create the Discord database idempotently on existing
Postgres volumes and render matching bridge/Synapse registrations. The helper
uses the hash-locked host dependencies in `requirements-host.txt`.

Open Beepa → Connections → Discord → **Connect (scan QR)**. Scan with the
Discord mobile app and approve. The QR expires after two minutes; Cancel
closes the helper's login connection. Reconnect restores a retained session;
Disconnect logs the bridge out without deleting imported conversations.

If Discord requires a CAPTCHA, complete sign-in in Discord and use **Use an
account token instead** in the card. Follow the linked upstream instructions
to find the token, but submit it only in Beepa's password field. Never send it
to a chat or to someone helping troubleshoot. The bridge's upstream docs warn
that personal-account use can result in account restrictions. This is not a
Discord bot integration.

## Supported scope and limits

The bridge imports up to 25 recent private channels at startup. Other private
channels appear when messages arrive. Initial history is capped at 100 messages
per DM; missed-message catch-up at 500. These are bounded defaults, not a promise
to import an entire account archive. Group DMs follow the bridge's DM path.
Operator changes to these values survive subsequent managed renders.

Beepa lists joined conversations below Discord → Direct Messages, including
group DMs. Server channels, thread/forum navigation, initiating new DMs,
voice/video, and Discord role administration are not included. The bridge
retains its restriction on sending to strangers until relationships are synced.

The Beepa composer sends text. Incoming attachments use the existing media
display and mirroring behavior. Advanced Discord formatting, interactive
components, reaction editing and thread controls are not promised by this UI,
even where the bridge itself supports them. Existing timestamp and deduplication
logic applies to history and mirror copies. Double puppeting represents messages
sent in the native Discord client as the configured local Matrix user.

Every new conversation starts Private. Share and Direct retain their existing
per-conversation meaning. Linking a Discord account never shares a conversation
with the master. Discord account credentials and bridge state remain local.

## Transport and recovery

The bridge runs on the private Compose network at port 29334 with no host port.
The existing loopback helper adds POST-only `/connect/discord/` actions:
`start`, `poll`, `cancel`, `token`, `status`, `reconnect`, and `logout`.
Origin, JSON, custom-header and loopback Host gates run before any bridge call.
Identity comes from the installation, not the request body.

HTTP provisioning uses curl config on stdin; the WebSocket uses a socketpair
relayed through `docker compose exec ... nc`. Credentials never enter process
arguments, browser storage, logs, or Matrix messages. The QR session is bounded,
kept in helper memory, and returned as a boolean pixel matrix drawn on a canvas.
Restarting the helper discards pending QR login. Bridge sessions persist in
`mautrix_discord`; config is in the gitignored `discord/` runtime directory.

Update backups cover the Postgres cluster and Discord runtime directory.
Adding Discord secrets preserves fingerprints of every existing secret.
Older-code rollback leaves imported state in place; stop `mautrix-discord`
before intentionally returning to a release without Discord. Do not drop its
database or restore old send ledgers as a rollback mechanism. If the operator's
homeserver appservice list uses inline YAML, convert it to a block list before
rendering; an ambiguous shape is rejected rather than rewritten.

## Validation and live acceptance

Run `tests/integration/run.sh --discord` for pinned bridge startup, repeat
database creation, legacy provisioning and configured-user appservice auth.
It creates marked disposable local/master stacks and never logs into Discord.
`tests/integration/run.sh 17_discord_dms` tests nested DM/group-DM consent,
authorship, timestamps, media copying, restart catch-up and revocation using
synthetic Matrix messages. Unit tests exercise the real WebSocket transport
against a synthetic process, QR expiry, cancellation, HTTP guards and config
preservation. These checks do not establish live provider delivery.

For the user canary, connect the account, choose an existing DM and group DM
whose participants agree to testing, and verify one incoming and outgoing text
in both Discord and Beepa. Confirm an incoming image, native-client own-message
alignment, bounded history and reconnect after a bridge restart. Only send test
messages after selecting and authorizing the recipient. Keep the conversations
Private unless deliberately testing Share or Direct. Live results belong in
beads epic `pm_mng-zjl`, with acceptance task `pm_mng-zjl.7` open until verified.

Upstream references: [authentication](https://docs.mau.fi/bridges/go/discord/authentication.html),
[configuration](https://github.com/mautrix/discord/blob/v0.7.7/example-config.yaml),
[provisioning](https://github.com/mautrix/discord/blob/v0.7.7/provisioning.go),
[features](https://github.com/mautrix/discord/blob/v0.7.7/ROADMAP.md).
