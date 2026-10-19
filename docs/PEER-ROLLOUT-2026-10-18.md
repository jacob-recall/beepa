# Upgrade David and Elliot from the older UI to the current Beepa release

This is the deployment handoff for the bots operating the **existing** Macs.
Target: **0.3.6**, state version **2**, readers **1 and 2**, wire **1**.
Use the exact published commit supplied by Jacob; do not assume a version tag exists.
Already-upgraded Macs should also read the
[iMessage importer patch instructions](IMESSAGE-IMPORTER-ROLLOUT-2026-10-18.md).
Also apply [native unread-state and master controls](IMESSAGE-UNREAD-ROLLOUT-2026-10-19.md), including its source-Mac acceptance gate.
Upgrade the central master first, then Elliot, verify, then David and verify.
Do not run the master-only steps on a teammate-only installation.

## Copy this instruction to each source Mac's bot

> Read `AGENTS.md`, `bd prime`, and `docs/PEER-ROLLOUT-2026-10-18.md` from the
> published release. Upgrade this existing Beepa installation through the
> documented source-Mac procedure. Determine whether it belongs to Elliot or
> David from its stored identity; preserve its accounts, sessions, state roots,
> sharing choices, send journals and signed iMessage CLI. Compare its installed
> commit with the supplied release SHA and verify the release source manifest.
> Handle ordinary path, service-label and managed-configuration differences
> using the installed manifest and supported updater. Enable the per-account
> CRM batch setting only at the documented activation gate. Verify the full
> source capability, suggestions, receipts, conversation tracking and ping
> behavior; report unavailable evidence honestly. Do not deploy another Telegram
> bot, copy master credentials, broaden sharing globally, reset/re-enroll an
> account, replay an uncertain send, or send test messages without a separately
> approved recipient. Return the evidence report at the end of this guide.

Jacob supplies the published SHA and which Mac is being updated. The bot can
discover the remaining installation facts locally. A real-message test needs
the account owner's chosen recipient and permission; Jacob's previous self-chat
tests are not permission to message someone from a different account.

## 1. What was actually audited

On October 18 this checkout's `HEAD` and **locally cached** `origin/main` were
`b73bb5f` (October 12, native iMessage receipts). At the initial audit, the CRM,
notification, control and ping work below was modified/untracked rather than
later local commits. The initial remote-head check failed because the agent
session could not resolve the Mac user. During publication preparation, an
authenticated connection using the existing GitHub key and strict host-key
verification confirmed remote `main` also matched `b73bb5f`. The final published
release SHA is supplied in the publishing handoff; use that exact SHA rather
than this historical audit baseline.

The peers' exact installed commits are unknown. “Since the UI updates” is not a
version identifier: record each peer's installed SHA before selecting changes.
This guide covers the older UI baseline plus every current runtime change in
the [source manifest](releases/2026-10-18-source-manifest.json).

### Changes the peer bots must accommodate

| Area | Previous / new behavior | Deployment responsibility and evidence |
| --- | --- | --- |
| Durable sync and original dates | Offline history/event queues, original message timestamps, fair retry/revocation; bridge generations remain distinct | Source uplink and retained databases. Do not reconnect orphan portal generations by name. Foundation commits `dcb0529`, `5d9522b`, `4f7fcbf`, `86e8325`, `169b872`. |
| Older UI / permissions / schedules | Triage Rail, explicit account default, unattended bridge invites behind the existing acknowledgment, and scheduled sends | Retain existing defaults, acknowledgments and schedules. Relevant commits `ac390f7`, `d50f590`, `cb2e95a`, `8871a82`, `c2211cf`, `e2d9d90`. Later explicit Suggest/Direct behavior below supersedes old automatic suggestion retirement. |
| Delivery/read receipts | Durable exact-message/part evidence; one gray check is delivery unconfirmed, two gray is delivered, two blue is recipient read | Both apps, uplink and iMessage daemon. Commits `1b594a8`, `199d85e`, `b73bb5f`; [receipt details](PEER-ROLLOUT-2026-10-12.md). Native acceptance is not delivery; our own reads and replies are not recipient reads. |
| Central CRM and contact picker | One master list across accounts; inline search/add from contacts already shared by each user; compact rows open the regular conversation view | `apps/master/crm-*.js`, `shared/model/crm.js`, `master/crm.py`, enrollment/gateway routes. A person can have multiple explicit account bindings with separate histories. No address-book scraping/import consent is added by this rollout. |
| Ordered permission changes | Listing a contact can enable Direct on its verified existing conversations, including an explicitly Private conversation | `crm_control.py`, `crm_policy.py`, `crm_resolver.py`, user `ordered-consent.js`. Source prepare/commit and exact acknowledgments required. Later manual Private wins. Removing the last list or archiving retains already applied Direct. Never globally set every contact Direct. |
| Batch outreach and new chats | Select contacts, personalize, review a frozen batch, then submit to the correct source accounts | Source `crm_batch.py` / `crm_approval.py`, user `crm-approval.js`. **Batch opt-in defaults off.** New conversations still require source-user approval. Currently identifier-only new-chat support is iMessage; other providers need an existing conversation. |
| Suggest versus Direct | Multiple independent queued suggestions with individual accepted/declined history; later conversation activity does not dismiss other suggestions | Both app entry points, `proposal-decisions.js`, `proposal-lifecycle.js`, `proposal_status.py`. Explicit Suggest stays a suggestion even in Direct rooms. Explicit Direct goes straight to dispatch; a failed gate produces refusal, never a fallback suggestion. Acceptance and transport outcome are separate. |
| Per-message response tracking | Link the next incoming response to the most recent unanswered outreach in the same source account, conversation and generation | `crm_tracking.py`, `conversation_tracking.py`, `crm_native_history.py`, `crm_provider_history.py`. Exact native ordering, correction audit, and conservative unknown/reconciling states; never match message bodies. |
| Lists drive monitoring | Waiting-for-me/them appears immediately; reminders require an explicit deadline; canonical lists own alerts, schedules, grouping/digests, channels and fallback | Master canonical CRM + controller. No mandatory quiet/working hours. Keep watched contacts together across apps. Do not create a second follow/reminder database as policy authority. |
| Mac browser notifications | API-driven Web Push, subscription/status/history and display/click acknowledgments | Master enrollment API, push helper, worker, HTML, nginx and gateway CSP. Browser permission remains a one-time user action. Provider acceptance is not proof of an OS banner. |
| Telegram / hosted assistant | One paired master bot; canonical lists/settings, history, reviewed sends, message deadlines, contextual replies and asynchronous model commands | Central master only: `control-core.mjs`, `control-domain.mjs`, `telegram-service.mjs`, routing adapters and persistent control state. Preserve the existing model/provider settings; no peer model keys or extra bot pollers. |
| Presentation | Readable names/app names, local dates/times, date sections for history, proper Telegram entities, no visible raw Matrix IDs/JSON | Master controller and its configured timezone/name overrides. Exact IDs remain internal. Literal conversation text and reviewed outgoing text are not rewritten by presentation cleanup. |
| Account ping | `/ping Elliot`, `/ping David`, `/ping all`; authenticated `action: ping` API | Source uplink reports verified native iMessage health separately from master-link health. Missing evidence is unavailable, not offline/healthy. Snapshot heartbeat check, not an active test send. |
| Deployment/recovery | State v2, additive local journals, staged code activation, retained control/browser state and recovery deduplication | `beepa_update.py`, `install_config.py`, `master/lifecycle.py`, `master/control_recovery.py`. Old state-v1-only releases cannot be rolled back onto this state. |
| Other integration changes | Discord private provisioning, auth/session fences, new gateway API routes and notification CSP | Already-published Discord commit `11c4699` plus current shared client/gateway changes. Preserve provider logins; connecting optional providers is not required. |

Design details: [CRM implementation and limits](specs/2026-10-12-master-crm-verification.md),
[CRM protocol and rollout](specs/2026-10-12-master-crm-design.md),
[provider-order evidence](specs/2026-10-12-crm-provider-order-evidence.md),
[browser notifications](specs/2026-10-13-master-notifications.md),
[Telegram/control/ping](specs/2026-10-17-telegram-master-control.md).
Mockups and the earlier WhatsApp design are design history, not deployment inputs.

## 2. Publishing gate — Jacob's checkout

Commit and push the **whole reviewed release**, including newly added modules,
tests/fixtures, these documents and `release.json`. `git diff --stat` alone omits
untracked modules. A push without adding those modules will ship broken imports.
Do not include `.env`, tokens, local sessions, databases/WAL files, VAPID keys,
control configuration, backups, or personal test artifacts. Those remain local.
Beads Dolt synchronization is separate from pushing code; this runbook works
without copying Jacob's local Beads database onto the peers.

Record the final full commit SHA and verify it on the actual deployment remote.
Run the following packaging check **after committing**, from the repository root:

```sh
python3 - <<'PY'
import hashlib, json, subprocess
from pathlib import Path
m = json.loads(Path('docs/releases/2026-10-18-source-manifest.json').read_text())
subprocess.run(['git', 'merge-base', '--is-ancestor', m['audited_base_commit'], 'HEAD'], check=True)
for name, expected in m['sha256'].items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, name + ': changed/missing working file'
    committed = subprocess.check_output(['git', 'show', 'HEAD:' + name])
    assert hashlib.sha256(committed).hexdigest() == expected, name + ': absent/different committed file'
print('Required runtime files are present in HEAD and match the audited snapshot.')
PY
```

The manifest is a completeness check, not a signature or substitute for tests.
If runtime code intentionally changes after this audit, review the change,
regenerate the relevant hashes, rerun affected tests and publish a new SHA.
Do not simply delete a failing manifest entry to make a partial release pass.
The updater stages `git archive HEAD`; uncommitted/untracked fixes are not deployed.

## 3. Preflight on each existing source Mac

Operate as the account's normal logged-in macOS user, not root or another user's
agent session. Confirm Docker is running and the existing Tailscale connection
can reach the configured master. An agent shell's DNS failure alone is not proof
that the running source service is disconnected; corroborate with its health.

From the existing checkout:

```sh
git status --short
git remote -v
git log -1 --format='%H %s'
python3 install_config.py --root . identity
```

Read `.beepa-install.json` privately. Record `install_id`, roles, local identity,
state/master-state/code/log roots, interpreter, Compose project names and CLI
path. Read `.beepa-update/installed.json` for the **active** SHA and state version;
checkout HEAD may not be active. With no manifest, the updater can adopt an
unambiguous existing installation; do not invent a new identity or run setup.

Capture protected local evidence: configured local/master user IDs, paired space,
sharing defaults and room overrides, provider login status, pending suggestions,
scheduled sends and uncertain/attempted outgoing work, native CLI hash/inode,
and current service labels/PIDs/code paths. Never print credentials or whole
environment/configuration files into the bot transcript.

Ensure a protected pre-update backup exists. The supported updater also stops
its writers and takes PostgreSQL dumps plus runtime/SQLite snapshots. Verify its
reported backup and checksums afterward. Do not use `cp` on an active SQLite
database and discard the WAL; use the existing backup API/workflow.

Preserve existing source state, especially `agents/uplink/state.db`,
`agents/contacts/contacts.db`, `imessage/state.db`, `imessage/daemon.json`,
local/master environment files, `.beepa-install.json`, provider volumes and the
signed CLI. No reset, volume deletion, account reprovisioning, re-enrollment,
manual schema deletion or replay of uncertain sends belongs in a normal upgrade.

## 4. Pull and activate the full source release

1. Compare the existing repository/fork with the canonical Beepa history.
   Fetch the operator-selected remote and fast-forward to the supplied SHA.
   For a clean checkout whose deployment branch tracks the intended release:

   ```sh
   git pull --ff-only
   git rev-parse HEAD
   ./update.sh
   ```

   Confirm HEAD is the supplied SHA; repeat the packaging check from section 2.
   A divergent fork or dirty tracked files needs a reviewed merge/port; never
   use `reset --hard`, `clean -fd`, or discard tenant-specific changes blindly.
   Move configuration-only differences into the existing managed configuration
   override mechanism. Genuine code adaptations must be committed, tested and
   reported as a separate derived SHA before activation.

2. Close stale user/master tabs before changing consent protocol. Keep CRM batch
   dispatch disabled during baseline checks. Preserve existing queued/scheduled
   work and inspect it; do not use deployment as permission to release old drafts.

3. Run the reviewed activation from that checkout:

   ```sh
   ./update.sh --apply
   ```

   The updater preserves the native executable, stages a committed tree, merges
   managed configuration and starts additive daemon migrations. Let the daemons
   create their own CRM policy/control/batch/approval/tracking and receipt tables.
   Do not hand-author capability events or generate replacement journal epochs.
   Existing proposals-room permission migration is performed by the source agent.

4. Check `.beepa-update/journal.json` is complete, the installed SHA matches, and
   service code paths point at the staged release. An interrupted update must be
   resumed with **the same release's** `./update.sh --apply`; do not pull a new
   release over an unfinished journal. Config conflicts are reviewed through
   `.beepa-config/conflicts/` and the process in [UPDATES.md](UPDATES.md).

5. Verify exactly one uplink and one iMessage daemon for this installation. The
   updater migrates legacy `com.jkali.*` labels to `org.beepa.*`; do not run both.
   Verify contacts import and the relevant connection helpers as well. If a
   previously missing/stopped source agent needs installation, use the current
   `install_config.py --root <existing-install-root> install-agent uplink` (or
   `imessage-daemon`) after confirming the manifest/code/state paths. Do not launch
   an extra background daemon to work around a broken GUI launchd session.

6. Reload the actual user app from this Mac's configured local URL and the
   central master app. Check newly imported modules return 200 and the user app
   discovers ordered consent/CRM approval support. A cached old browser writing
   account-data permissions directly must not remain active alongside the ordered
   writer. Keep source policy-control failures visible; never bypass the writer.

7. Compare protected pre/post evidence: same installation/account/link, CLI
   hash and inode, provider logins, state roots and send journals. Investigate
   any identity difference before enabling CRM. Do not copy Jacob's or the
   other peer's runtime data to make a check pass.

## 5. Enable and verify CRM on one account at a time

The source opt-in is **`UPLINK_CRM_BATCH_ENABLED=1`**. It defaults to `0` even
when all new code is installed. Read-only discovery, ordered manual consent,
cancellation and reconciliation must remain working with batch dispatch off.

After baseline checks pass and outstanding attempts/queued work have been
accounted for, set that single key in this installation's existing protected
uplink environment. `run-uplink.sh` reads `local.env.local`, then
`uplink.env.local`; the latter wins. Edit in place with a literal config parser,
retain mode 0600 and all other values, and avoid duplicate conflicting entries.
Restart the **verified existing uplink service**, not another instance. Do not
alter account-wide sharing defaults, invite acknowledgments, or Direct rate caps.

Verify the source-authored proposals-room `com.jkali.crm.capability` is for the
expected owner, with source-created generation/journal epoch (retained if already
present; created by the source on its first CRM migration) and:

```text
schema: 1
ordered_policy: true
direct_prepare_commit: true
direct_cancel: true
batch: true
batch_cancel: true
conversation_tracking: true
```

Missing capability means “update needed,” not permission to fabricate it on the
master. Verify both local and master copies through the normal adapters.
The master must recognize the upgraded account before proceeding to the other.

On an owner-selected contact/list, verify the following boundaries without
launching an outreach campaign:

- Search offers only shared contacts. Account/channel assignment stays explicit;
  opening a different history tab does not silently change the sending account.
- List addition warns about Direct, waits for source preparation and applied
  acknowledgments, and disables sending while permission is unresolved. This
  specific addition can change existing Private conversations to Direct.
- A later manual Private wins. Removing/archiving a list does not undo applied
  Direct. Undo in the inline picker removes membership, not the permission grant.
- New-chat items require the user's exact approval. A canceled/changed/expired
  item must not send. The iMessage first start-chat send may remain uncorrelated
  with exact native receipt/reply IDs; do not invent an ID or resend it.
- Multiple Suggest requests remain separate queued items with per-message
  accepted/declined history. Accepted does not necessarily mean sent. Direct
  requests never appear as actionable suggestions, including when refused.

Live approval/send steps need a separately approved test target. Use the isolated
tests below for the send/restart/replay paths before any such approval.

## 6. Central master steps — not peer-Mac setup

The central master supplies CRM metadata, control, notifications and Telegram.
Elliot/David require no Telegram/OpenRouter keys or private master authority files.
Never start a second poller for the existing bot token.

1. Before maintenance, stop initiating new sends and let active command/review
   submissions settle. Preserve pending/uncertain outcomes. Check authenticated
   control status, active API commands, Telegram inbox and delivery queues.

2. Identify `beepa-control` by immutable container ID **and** its `/state` bind
   mount resolving to this master's `control.local`. The helper
   `MasterOps.control_worker()` in `master/lifecycle.py` performs that check.
   Stop that verified worker before the general updater takes its backup.
   Record its prior running state for recovery. Do not stop a same-named worker
   belonging to another state root.

   **Current updater gap (`pm_mng-f3w`):** `beepa_update.py` stops Compose services
   and launch agents but does not itself quiesce this standalone worker. Its
   later `setup-control.py` call recreates the worker. Do not rely on the general
   update command alone for controller quiescence. The separate master lifecycle
   backup path already verifies/stops this worker, but an old active release may
   lack that implementation. Do not nest an unfinished lifecycle operation inside
   an update or restore a database merely to upgrade code.

3. Preserve `notifications.local/{notifications.db,vapid.pem}` and
   `control.local/{control.db,config.json,enabled}`, all mode 0600 under private
   directories, in addition to normal master state. Retain Telegram pairing,
   bot identity, deduplication/unknown outcomes, VAPID keys and browser devices.
   Full database recovery has different quarantine/re-pairing rules; it is not
   this ordinary update. See `master/control_recovery.py` and the control spec.

4. Explicitly provide the **recorded** runtime locations to the master updater:

   ```sh
   : "${BEEPA_MASTER_RUNTIME_ROOT:?Set the recorded master installation state root}"
   : "${BEEPA_MASTER_DATA_ROOT:?Set the recorded master data directory}"
   BEEPA_INSTALL_ROOT="$BEEPA_MASTER_RUNTIME_ROOT" \
   BEEPA_MASTER_STATE_DIR="$BEEPA_MASTER_DATA_ROOT" ./update.sh --apply
   ```

   The two variables above must first be filled from the existing installation
   manifest/resolved runtime paths, not copied from Jacob's Mac. This also keeps
   the setup-control subprocess pointed at retained state when code is staged.
   Confirm `master/.env` still privately contains the existing bot/model keys;
   preserve the selected model, timezone and display-name overrides. An existing
   config triggers worker setup; the updater does not configure a brand-new bot.

5. From the **activated source**, build the updated helper with
   `master/setup-notifications.sh` (Docker available). It builds a restricted
   helper image; it does not replace the retained VAPID key. For first-time control
   setup only, install the Node 22 Alpine runtime and run
   `master/setup-control.py` with the same recorded environment and configured
   interpreter. First verify container ownership, because this installer uses
   the fixed `beepa-control` name. Do not rerun setup merely to obtain a new pairing.

6. Ensure enrollment, gateway, web assets and worker all use the same activated
   release. Upgrade enrollment routes before enabling the new UI/controller;
   restart enrollment after first control enablement. Keep HTML, nginx and
   gateway CSP aligned, including `beepa-notification-worker`. Check actual
   notification/CRM JS and service-worker URLs, not just the index page.

7. Through the existing manager-authenticated API, check:

   ```json
   {"action":"status"}
   {"action":"lists"}
   {"action":"ping","account":"all"}
   ```

   These are separate requests to `POST /admin/control` on the configured master
   origin (locally enrollment port 8019; gateway remotely). Use credentials from
   the existing private configuration, never in argv, URLs or logs. Expect
   `domain_ready`, `transport_ready`, preserved pairing, fresh monitoring and no
   setup/poll errors. A 200 from `/health` alone is not this acceptance check.

8. Confirm browser notification permission/subscription on the master's device.
   Only the browser/user can grant permission. Use the notification API for an
   authorized test and distinguish queued, provider-accepted and browser-shown
   evidence. macOS notification settings/Focus can still suppress a banner.
   Existing subscriptions should reconnect after refresh with the same VAPID key.

9. Lists are the policy authority. The controller performs the idempotent
   `control_dispatcher` handoff; do not re-enable legacy independent discovery
   or create a second reminder scheduler. Preserve list channels, explicit
   deadlines, schedules, repeats, digest/grouping and primary/fallback settings.
   Hosted models prepare reviewed sends; they cannot confirm their own drafts.

## 7. Acceptance and diagnosis

Run from the target source with the configured Python/dependencies. Unit tests
and integration fixtures use synthetic accounts; they never need peer credentials.

```sh
tests/run.sh
tests/integration/run.sh 24 25 26 27 28 29
tests/integration/run.sh --notifications
```

The numbered scenarios cover receipts, native receipt reconstruction, canonical
CRM and ordered source controls. The notification scenario also tests the real
control API and source-owned ping evidence. Use only this generated disposable
harness; never provision test users on the live master. If a Mac lacks test
dependencies, record the omission and use the same-SHA qualification evidence;
still perform local live read-only checks. Optional actual-app browser fixtures
are documented in `tests/CLAUDE.md` and intercept every network request.

Run `python3 tests/integration/probe_propagation.py --passive` with the configured
runtime. It sends nothing. Check source logs and `/ping ACCOUNT` from the central
bot/API after both ends are updated. Wait for at least one new report after the
restart, rather than accepting a cached pre-update heartbeat.

| Check | Expected result / honest limit |
| --- | --- |
| Account mapping | Correct source owner and master space, unchanged tokens/identity, source-authored capability; no inference from display name alone |
| Sync | Recent master report and successful source ingestion; queues progress; an old last delivery is normal on an idle account |
| Native iMessage | Successful recent poll and no stuck import queue. Native health discovery uses existing full/incremental sync and verified management state; no CLI reinstall |
| Ping timing | Reports refresh about five minutes plus propagation. Master heartbeat/ingestion is stale after seven minutes; native report after eight. Not an active end-to-end canary |
| Missing peer health | “Unavailable/not verified.” Diagnose source version, service/session, source-space publication and connectivity; do not declare it healthy or definitely offline |
| Historical errors | Separate old incomplete imports from current queue/liveness. Do not delete refused rows or send ledgers to make status green |
| CRM response link | Most recent unanswered outgoing, same account/conversation/generation, exact source evidence. Missing/gapped/ambiguous evidence remains reconciling |
| Non-iMessage tracking | Audited running bridge image/schema and exact provider mapping required. Observed cache history is not complete upstream history; don't remove image checks to force success |
| Receipts | Gray/blue per exact target/part; no blue from our own read or an incoming reply. Disabled/unsupported recipient receipts remain unknown |
| Browser notifications | Authenticated API + connected subscription + browser display acknowledgment. A synthetic test is not proof of a visible macOS banner |
| Telegram | One paired operator/poller, readable names/times, queued summaries revalidate sharing, `/ping all` works without the hosted model |

No routine migration should change Apple privacy grants. Retain the signed
`imessage-cli` identity and use its JSON output; `isDelivered` and positive
numeric `seen` for outgoing one-to-one parts are relevant receipt evidence.
Do not use another Messages database, replace the CLI, or infer success from
the Python process's Accessibility state. Native group-read support is not promised.

For an explicitly approved self-test only, follow the exact resumable command in
the [receipt rollout](PEER-ROLLOUT-2026-10-12.md#native-imessage-acceptance-on-each-peer-023).
Keep the private report to resume observation without another send. A duplicate
test send is not a valid recovery strategy for an ambiguous outcome.

## 8. Failure handling and rollback

- A new UI with old source code is not a completed deployment. Keep unsupported
  actions disabled; update the full source release and reload old tabs.
- On CRM failure, set `UPLINK_CRM_BATCH_ENABLED=0` and restart the verified
  source uplink. This disables new batch dispatch while retaining ordered consent,
  cancellation and reconciliation. Applied Direct permissions remain until
  manually changed; disabling CRM is not a permission rollback.
- Preserve unknown/attempted outcomes and immutable checkpoints. Cancel only
  unattempted work through the normal acknowledged flow. Do not replay a batch,
  clear a cursor or restore an older send database to retry it.
- State version 2 deliberately blocks `--rollback` and applying releases that
  only read state version 1. Do not edit release metadata to bypass this. Use a
  tested compatible state-v2 repair release. Full disaster recovery is a separate,
  reviewed operation with the retained deduplication/authorization safeguards.
- First adoption from an unmanaged checkout has no automatic prior managed
  release to return to. Keep the pre-update evidence and protected backup.
- If launchd, Docker or Git access fails because the bot is outside the owner's
  normal login session, report the exact failure and resume in that session.
  Do not spoof user identity or change file ownership broadly to bypass it.

## 9. Report separately for Elliot and David

Return a concise report containing:

1. Previous active SHA → published target SHA → actual activated SHA/code path;
   any local adaptation and its separate tested commit.
2. Preserved installation/account/link/CLI fingerprints and correct state roots;
   backup location/checksum verification (no secrets in the report).
3. Exactly one active daemon/uplink, current source capability, batch setting,
   and fresh master/native health with pending/error counts.
4. Checks actually run: automated, UI, API and any separately authorized native
   observations. Clearly distinguish skipped, unsupported and uncertain results.
5. Master connectivity, central list presence, suggestion decision history,
   response tracking and notification/ping results. Remaining blockers and owner.

Rollout tracking: existing CRM peer rollout `pm_mng-pvs`, ping peer rollout
`pm_mng-51b`, master updater gap `pm_mng-f3w`. Use the local repository's Beads
workflow; if those records aren't synced, create a local rollout record and cite
these references rather than treating `.beads/issues.jsonl` as the sync protocol.

As of this audit, Jacob's local master/source passed the full discovered suite,
the isolated notification/control scenario, production authenticated ping and a
passive propagation check. Local iMessage polling worked; five receipts and
historical incomplete updates remained reported. Elliot/David health was
unavailable. None of that is evidence that either peer has been upgraded or that
their native receipt/UI acceptance has passed.
