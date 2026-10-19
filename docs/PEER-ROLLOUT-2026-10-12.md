# Receipt reliability rollout for Elliot and David

**Historical receipt release instructions.** For the current CRM, notifications,
Telegram and ping rollout, start with the
[October 18 upgrade guide](PEER-ROLLOUT-2026-10-18.md). Its state-version-2
compatibility and deployment instructions supersede the version-1/rollback
guidance below. Native receipt semantics and the authorized self-test remain useful.

Release: **0.2.3**, tag **v0.2.3**, canonical repository
`git@github.com:jacob-recall/beepa.git`. Run this procedure on each person's
source Mac, one at a time. These are instructions for the bot operating that
installation; publishing this file does not mean either peer has been upgraded.

## What this release changes

Both apps render the requested gray/blue checkmarks on outgoing messages.
Version 0.2.3 adds real outgoing iMessage delivery/read evidence from the existing
signed engine, including durable recovery and fresh-browser-sync reconstruction.
The detailed semantics and provider limits are below.


- The uplink coalesces receipt updates in SQLite `receipt_outbox`, one row per
  shared room. It commits before advancing the local sync cursor. A master
  outage leaves the row pending, and the independent `receipts` stage retries
  after reconnection or a daemon restart without needing another receipt.
- Teammate and correspondent timestamps advance independently. Partial updates
  cannot erase the other side. Duplicate/older timestamps do not trigger writes.
- Every publication rechecks the current link, room mapping, and Share/Direct
  consent. Revocation deletes pending receipts; destination changes clear the
  old queue. Full reconciliation reseeds existing receipts for new mirrors.
- A delayed browser mark-read response cannot clear a newer unread count or
  change another login session. Receipt updates waiting for the master appear
  in its health line and `receipts_pending` diagnostics.
- The previously uncommitted Discord loopback provisioning module, route wiring,
  and tests are included. Existing sessions are retained; connecting Discord is
  optional and is not part of this receipt rollout.

The schema addition is additive; state and wire compatibility remain version 1.
Older code ignores the outbox and does not provide its recovery guarantees.
No provider credentials, personal messages, databases, or local session files
are distributed in the release.

## Known limits: report these accurately

Unread badges use Matrix `notification_count`, not a local count of all unseen
messages. Opening a validated conversation posts both `m.fully_read` and
`m.read` when the browser's receipt preference is enabled (default on).
Disabling that preference also disables this automatic mark-read action.

The other person's receipt only exists when that provider and bridge supply it.
The synthetic integration tests prove the Matrix/app/uplink path, not receipt
support on every real provider or an actual recipient's actions. In group chats,
the newest receipt from an eligible participant does **not** mean everyone read.

**Checkmark behavior (0.2.3):** one gray check means sending / delivery
unconfirmed; two gray checks mean explicit recipient delivery; two blue checks
mean an eligible recipient supplied a read receipt for that exact message ID.
Bridge acceptance alone stays one check. Failed sends show an error indicator.
Group checks mean at least one recipient, as the accessible tooltip explains.

Receipts match exact target IDs, mapped through the current mirror generation.
They never infer read position from receipt time, displayed timestamps, or
historical corrections. This fixes pm_mng-m07's false positive. The conservative
tradeoff is that messages without their own evidence can remain gray even if a
later message has a receipt. The user view reconstructs available receipt/status
evidence from Matrix sync/history on reload; it does not invent older receipts
that a provider no longer exposes. The master retains published per-event state.

Matrix bridge status events are enabled by the managed templates. Only a status
from that conversation's bridge bot is accepted; `SUCCESS` alone is not delivery.
A nonempty remote `delivered_to_users` is delivery evidence. The iMessage daemon
publishes its explicit native `engine_delivered` result through a separate,
idempotent status publisher. Plain engine acceptance stays unconfirmed. The
iMessage adapter now requests JSON, retains the returned native message IDs,
and reconciles outgoing `isDelivered` and numeric `seen` timestamps. A read
confirms delivery. Local `isRead`, own Matrix receipts, and native acceptance
never establish recipient read. The pinned engine supplies read timestamps for
one-to-one chats only, and the recipient must enable Apple read receipts.
Do not promise iMessage group read receipts or infer unknown historical sends.

The additive `message_receipts` SQLite table retains per-target evidence before
sync advances. The independent `message_receipts` stage maps to the current
master event and retries after outage/restart, rechecking link and consent.
Unknown targets stay queued instead of attaching to a different message.
Private/revoked rooms and destination changes clear that queue. The existing
room receipt outbox now also mirrors Matrix's actual `unread_count` (including
zero); the master no longer estimates unread badges from receipt timestamps.

Native iMessage receipts are stored per exact native part in `native_receipt`,
with publication acknowledgments in `published_native_receipt`. A multipart
Beepa send advances only when all mapped parts confirm that state. Bounded
targeted native reads recover changes outside chat tails, with backoff for
missing/disabled receipts. Trusted bot state `com.jkali.imessage_receipt`, keyed
by the original Matrix event, retains evidence through browser refresh. The
uplink forwards it through the existing consent-gated per-message pipeline.
These tables are additive; the signed executable and its permissions remain
unchanged. Polling is the current implementation; no persistent native event
subscriber is needed for correctness.

The iMessage `published_message_status` ledger prevents duplicate publication;
a Matrix failure retries the status only, never the native send. These schema
changes are additive; unread counts use a separate `room_unread` table so the
0.2.1 receipt table shape remains compatible. Rollback cannot remove already-published Matrix state;
older releases ignore the new receipt states and retain their old limitations.

## 1. Identify and preserve the installation

Start from the existing Beepa checkout. Read its `AGENTS.md` and run `bd prime`.
Use that repo's existing tracker for the local rollout and record whose Mac it is.

```sh
git status --short
git remote -v
python3 install_config.py --root . identity
```

Inspect `.beepa-install.json` for `install_id`, `local_localpart`, `roles`,
`state_root`, `code_root`, `logs_root`, `python_path`, Compose project names,
and `imessage_cli_path`. Do not print tokens, `.env` files, or bootstrap JSON.
If no manifest exists, let the supported updater adopt existing state; do not
invent an identity or run a fresh setup. If identity is ambiguous, stop for review.

Retain Elliot's or David's configured account, current organization pairing,
sharing choices, bridge sessions, native executable, and send ledgers. Never
copy Jacob's `.beepa-install.json`, tokens, account defaults, or source-room
overrides. In particular, Jacob's documented “everything Direct” policy is
**not** a peer migration step. Do not create a personal master on a teammate-only
installation or change its recorded roles.

Before activation, record locally: native CLI path/hash/inode; account IDs;
current sharing policy; active release; service status; and queue counts.
Use `beepa_update.inventory()` for protected local fingerprints if useful.
Confirm Docker is running, Tailscale is authenticated/connected, and the configured
master health endpoint is reachable **from the service's real user session**.
In Jacob's agent shell, DNS and `scutil` falsely reported failure while the live
uplink successfully published to the master; corroborate with fresh service logs
and master-received health before asking the owner to reconnect.

## 2. Fetch the published release without losing local work

If `origin` is the canonical repo, use it. If this is a fork, use its existing
canonical `upstream`; add that remote only if absent and its URL is verified.
Do not overwrite the fork's origin. The commands below use the chosen remote:

```sh
git fetch <canonical-remote> tag v0.2.3
git show v0.2.3:release.json
git rev-parse 'v0.2.3^{commit}'
```

Confirm the tag's commit matches Jacob's release handoff. Inspect tracked local
changes and commit/reconcile intentional custom work before deployment. Do not
reset, discard, force-push, automatically stash, or merge unrelated histories.
For a clean branch that is an ancestor of the release:

```sh
git merge --ff-only v0.2.3
git status --short
git rev-parse HEAD
```

HEAD must equal the reviewed release commit. An ahead/divergent branch requires
an explicitly reviewed integration; do not run the updater on an arbitrary tree.

## 3. Activate the code actually used by services

```sh
./update.sh
./update.sh --apply
```

The first command only inspects. The second stages the **committed** release,
backs up runtime state (including SQLite WAL through the backup API), migrates
configuration, restarts the recorded roles, and checks existing accounts.
Pulling Git alone does not update a managed installation's active services.
Confirm the resulting manifest `code_root` and running process paths point to
the new staged release, and local static assets come from it.

If interrupted, retain `.beepa-update/journal.json` and rerun the same release's
`./update.sh --apply`; do not pull another target over an unfinished update.
Do not use `reset.sh`, delete Docker volumes, reprovision users, re-enroll, rebuild
the archive, or replace/re-sign/move the authorized iMessage executable.
Resolve configuration conflicts using [UPDATES.md](UPDATES.md). If launchd,
Docker, or Tailscale cannot be controlled from the bot's shell, ask the owner for
that specific desktop action and verify the live service afterward.

Historical timestamp repair is separate: follow [TIMESTAMP-REPAIR.md](TIMESTAMP-REPAIR.md)
if explicitly requested. Do not combine a history repair with receipt validation.

## 4. Validate before moving to the second Mac

Use the installed Python interpreter from the manifest; Node must be available.
From the updated checkout, with `BEEPA_PYTHON` set to that recorded interpreter:

```sh
"$BEEPA_PYTHON" tests/run.py
tests/integration/run.sh 24_read_unread 25_receipt_outage_recovery 7_read_only_manager
```

The integration runner creates its own marked local/master stacks and synthetic
accounts. It never stops the production master or sends provider messages. Never
run the harness directly against production URLs or create test users on the
live master. The tests cover:

| Check | Expected evidence |
|---|---|
| Actual app receipt writer and parser | Synthetic unread count 0 → 1 → 0 |
| Master state | Teammate unread → read; correspondent update retains teammate position |
| Restart | New receipt publishes after restart; prior state retained |
| Master outage | Receipt is queued before cursor advance, survives process restart, publishes after recovery with no new receipt |
| Revocation | Pending receipt is discarded when the conversation becomes Private |
| Browser lifecycle | Disabled/invalid writes suppressed; failures and stale responses preserve unread state |
| Manager authority | Manager cannot send a message into mirrored rooms |

Then verify local app HTTP 200, local Matrix `/health`, helper health, iMessage
health, and the configured master endpoint. Inspect `com.beepa.sync_health` in
the local account and `com.jkali.uplink_health` on that teammate's master space:
fresh `updated_at`, `connected: true`, no sustained `receipts` stage error, and
`receipts_pending` draining to zero. Check production logs and confirm there is
one running uplink, not a duplicate legacy/new launch agent. Container “Up” or
HTTP 200 alone is not evidence of authenticated synchronization.

Reload both apps to refresh modules. In an owner-selected test conversation,
observe an incoming message's unread badge, open the conversation, and confirm
it clears and the master's read state catches up. Refresh both pages and repeat
after restarting the local uplink. Real-provider receipt acceptance needs that
provider to emit a receipt; record unsupported/missing receipts as such. Do not
send a message to a real person without an explicitly approved test destination.
Do not mark unrelated personal conversations read as a test.

## 5. Report and rollback

Report separately for **Elliot** and **David**: fetched commit/tag, active release
path, preserved identity/consent/native CLI fingerprints, test results, fresh
health age and pending receipt count, actual provider/browser observations, and
any unmet acceptance step. A successful pull is not a completed rollout.

If the code release must be reversed, use `./update.sh --rollback` only when a
previous compatible managed release exists. This changes code; it does not roll
back account state or send outcomes. Retain the protected backup and journal.
The first upgrade from an unmanaged checkout has no automatic previous-release
rollback guarantee. Older uplinks do not retry the new receipt outbox.

## Evidence from Jacob's installation

On 2026-10-12 the full discovered unit/conformance suite passed, including
122,429 consent vectors and 8,112 invite vectors. Disposable scenarios 24–27
passed. Scenario 27 runs the actual native-response parser, daemon, two
homeservers, uplink, and user receipt parser, with only the Apple CLI replaced
by synthetic responses. It covers acceptance, delivery, read, exact targeting
for identical texts, an outage, daemon/uplink restarts, and fresh sync with the
receipt outside the recent timeline.

One owner-authorized real iMessage self-test confirmed a native read timestamp,
local delivered/read state, and matching master state. The result survived
daemon/uplink restarts, with the signed CLI hash/inode unchanged. The diagnostic
client used the same master's loopback endpoint because its configured hostname
was unavailable from the test shell; the running uplink published normally.
This proves this installation's self-chat path, not another recipient's disabled
receipts or either peer's account. Browser visual acceptance remains required
on each peer's Mac.

## Additional 0.2.3 verification on each peer

After `./update.sh --apply`, verify the active bridge configurations have
`matrix.message_status_events: true` (Discord uses its existing bridge setting),
and that the updater restarted changed bridges plus uplink, iMessage, and the
master gateway where that role is installed. Never print entire configurations;
they contain credentials. Reload both web apps to pick up the versioned assets.

Run `python3 tests/run.py` and `./tests/integration/run.sh 24 25 26 27` from the
activated source when that Mac has the required test dependencies. The harness
creates disposable servers and cleans up only its own marked resources.
Scenario 26 verifies acceptance versus delivery, a late read of an older message,
a forged status on the newer message, and restart persistence. Do not substitute
production accounts into these tests. Check the live queues/errors separately.

For owner-approved provider acceptance, observe a new outgoing message progress
through the checks in the existing conversation. Have the recipient acknowledge
delivery/read themselves where supported; never manufacture a receipt or send
unrelated messages to claim acceptance. Record platform, observed states, and
any state the provider did not expose. This synthetic suite does not prove
actual recipient delivery on every provider.

Bridge delivery interpretation follows the upstream [mautrix status event contract](https://github.com/mautrix/go/blob/main/event/beeper.go).


## Native iMessage acceptance on each peer (0.2.3)

Preserve the configured signed CLI; do not rebuild/replace it, reset TCC, or
infer send/read results from a different Messages database. Activate the release
with the managed updater and reload both apps. Confirm the daemon and uplink
are using the activated source. Existing unknown sends are not retried.

With the account owner's explicit approval, use its configured self handle:

```bash
python3 tests/live/imessage_receipts_verify.py \
  --recipient '<configured self handle>' \
  --output /tmp/beepa-imessage-receipt-test.json \
  --i-am-sending-to-myself
```

This sends one clearly labeled message to the existing self portal. Open it in
Apple Messages with read receipts enabled. The report must confirm native,
local, and master read/delivery, plus an unchanged executable fingerprint.
Rerunning with the same report resumes observation without another send. An
ambiguous send report fails closed. Preserve the report for the restart check;
keep its identifiers private. The self portal must already be shared/mirrored
for the master check; the diagnostic never changes consent.

If the master also runs on that same Mac, an optional
`--master-loopback-url http://127.0.0.1:<published-master-port>` verifies through
its loopback endpoint and checks the authenticated master identity. This is a
diagnostic override only; do not change organization configuration or TLS.

Repeat observation after daemon/uplink restart and after refreshing each app.
Confirm the tested outgoing message has two blue checks. With a separately
approved recipient, also verify that delayed delivery remains one gray check,
confirmed delivery becomes two gray, and receipts disabled never produces blue.
Opening an incoming message in Beepa and sending OUR read receipt back to Apple
is a separate feature; this release displays the other person's read of our
outgoing message.
