# Receipt reliability rollout for Elliot and David

Release: **0.2.1**, tag **v0.2.1**, canonical repository
`git@github.com:jacob-recall/beepa.git`. Run this procedure on each person's
source Mac, one at a time. These are instructions for the bot operating that
installation; publishing this file does not mean either peer has been upgraded.

## What this release changes

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

**Exact read-position accuracy remains incomplete (pm_mng-m07).** The current
caption/master logic compares receipt wall-clock times to displayed message
times, rather than carrying the receipt's target event ID. A late receipt for an
older event, historical import, or clock skew can overstate which message was
read. Treat “Read”/“has read everything” as approximate. The durable queue fixes
loss and regression of the received state; it does not resolve this semantic
limitation. Do not advertise exact per-message or all-participant read proof.

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
git fetch <canonical-remote> tag v0.2.1
git show v0.2.1:release.json
git rev-parse 'v0.2.1^{commit}'
```

Confirm the tag's commit matches Jacob's release handoff. Inspect tracked local
changes and commit/reconcile intentional custom work before deployment. Do not
reset, discard, force-push, automatically stash, or merge unrelated histories.
For a clean branch that is an ancestor of the release:

```sh
git merge --ff-only v0.2.1
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
122,429 consent vectors and 8,112 invite vectors. Disposable scenarios 7, 24,
and 25 passed. Scenario 24 uses the actual app receipt writer/parser against
real Synapse; scenario 25 stops only the disposable master. No real-provider
messages were sent. Browser visual acceptance was unavailable in the agent
environment and is still required on each peer's Mac.
