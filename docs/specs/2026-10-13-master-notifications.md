# Master conversation notifications

The master follows shared conversations across managed accounts. A durable reader watches the master mirrors and sends Web Push to connected browsers. The UI and automation use the same authenticated API. No computer automation is involved in following, sending test alerts, checking new messages, or inspecting notification delivery.

## Use

This component is installed on the master host. Elliot and David keep using their existing user/bridge/uplink installations; they do not need notification credentials.

1. Build the push helper on the machine running the master: `master/setup-notifications.sh`. Docker must be running. The helper uses pinned dependencies from `requirements-notifications.txt`; the enrollment process stays standard-library Python.
2. Restart the master enrollment service after installing these files. Include `notifications.py`, `notify.py`, the helper Dockerfile, all master frontend assets, and the gateway/static-server configuration. Reload nginx after changing `views/nginx.conf`; changing the HTML policy alone is insufficient.
3. Open the master console, choose **Notifications → Enable browser notifications**, and allow the browser prompt. This permission/subscription is the only browser-owned setup step. It cannot be granted by a server API. HTTPS is required except for localhost. On this Mac the console is `http://127.0.0.1:8011/apps/master/index.html#notifications`.
4. Open any shared conversation and choose **Follow**. The notification panel switches between incoming messages (default) and all messages, manages the browser, sends tests, and shows history with delivery evidence. This is the same regular conversation viewer reached from CRM contacts and batches.
5. Clicking an alert opens its exact message in the regular master viewer. Opening notification history acknowledges only that notification, never native iMessage/WhatsApp read receipts.

The server continues watching with the console tab closed. Actual delivery depends on the browser/OS running or supporting background delivery, network availability, notification permission, and Mac notification/Focus settings. Fully quitting the browser is not a tested delivery guarantee. Android can use the same Web Push path but has not been hardware-tested here.

## API and command-line client

All management actions use `POST /admin/notifications` with `Authorization: Bearer <manager-token>` and a JSON object. Authentication checks the configured manager against the master homeserver **before** parsing the body. Managed-user tokens cannot call it. Through the master gateway use its HTTPS origin; legacy local enrollment uses `http://127.0.0.1:8019`.

`python3 master/notify.py` is a thin API client. Locally it reads the configured manager token without printing it or placing it in process arguments. For a remote `--base`, supply `BEEPA_MANAGER_TOKEN`; local credentials are never automatically sent to a custom URL. Do not paste credentials into shell commands, reports, or chat.

```sh
python3 master/notify.py status
python3 master/notify.py follow --room '!shared-room:master'
python3 master/notify.py follow --room '!shared-room:master' --all-messages
python3 master/notify.py sync
python3 master/notify.py test --request-id my-test-001 --wait-for shown --timeout 45
python3 master/notify.py delivery --id '<notification-id>'
python3 master/notify.py unfollow --room '!shared-room:master'
python3 master/notify.py unsubscribe --device '<device-id>'
```

| Action | Fields | Result |
| --- | --- | --- |
| `status` | none | Follows, latest 100 history entries with delivery evidence, device IDs/enabled flags, last poll and sync error. No subscriptions, tokens or message bodies. |
| `client_status` | `client` UUID, `stage`, `permission`, optional `error_code` | Bounded setup diagnostics, visible through `status.clients`: registration stage and error class only, never error strings or subscription URLs. Used by the UI to diagnose setup through the API. |
| `key` | none | VAPID public key for browser `PushManager.subscribe`. |
| `follow` | `room`, optional boolean `all_messages` | Verify a managed, currently shared mirror and begin following at its current cursor. Updating mode preserves the cursor. |
| `unfollow` | `room` | Remove follow and cancel unsent/retrying alerts for that room. |
| `subscribe` | `subscription` | Register the browser-generated PushSubscription JSON; returns stable `device` ID. Re-registering is safe. |
| `unsubscribe` | `device` | Disable this endpoint and cancel its pending alerts. |
| `test` | optional `device`, optional `request_id` | Durably queue a clearly labeled test, return `notification_id`. No device selects all enabled devices; zero devices fails explicitly. Reusing the same request ID and target returns the same notification without sending again. |
| `sync` | none | Run the same poll/outbox consumer as the background worker and return status. There is no parallel second send path. |
| `delivery` | `id` | Retrieve one history entry and its per-device evidence, including older entries outside recent history. |
| `seen` | `id` | Mark notification history as seen; makes no Matrix/native writes. |
| `control_enqueue` | `entries` | Canonical-control adapter: queue up to 50 verified source events with exact list-revision references. Requires canonical control; retries preserve notification IDs. |
| `control_dispatcher` | none | Atomically transfer source discovery to the canonical controller. Returns the same durable handoff ID and frozen room cursors on retry; never calls back to the controller. |
| `control_outcomes` | `ids`, up to 100 | Enabled-device count and aggregate/per-device delivery evidence, without subscription endpoints, receipt tokens or message text. An empty ID list reads availability only. |

Example request bodies (send with the authenticated API client above):

```json
{"action":"follow","room":"!shared-room:master","all_messages":false}
{"action":"test","device":"<device-id>","request_id":"deployment-check-001"}
{"action":"delivery","id":"<notification-id>"}
```

A test request acknowledges **queueing**, not browser display. `--wait-for accepted|shown|clicked` polls delivery evidence and exits nonzero on timeout. The UI renders the same evidence:

| Evidence | Meaning |
| --- | --- |
| `state: pending` | Queued; attempts greater than zero mean retrying. |
| `state: sending` | Atomically claimed before the provider call; another worker cannot send it concurrently. |
| `state: accepted` | Push provider accepted the encrypted request. This alone does not prove device delivery. |
| `state: unknown` | A provider response was lost or the worker stopped during delivery. No automatic resend. A valid late browser receipt can reconcile it. |
| `state: failed` | Explicit nonretryable rejection, or five attempts exhausted for retryable HTTP failures. |
| `shown_at` | Service worker reports that `showNotification()` resolved successfully. This proves browser acceptance of display, not that a banner was visible through Focus. |
| `clicked_at` | Browser reports the notification click. |
| `state: expired` | Retry age exceeded one hour, or provider returned 404/410. |
| `state: canceled` | Follow/subscription removed or sharing revoked before send. |
| Empty deliveries | History recorded while no browser was enabled. Connecting later does not replay old history. |

`POST /admin/notifications/receipt` accepts only `{token,stage}` where stage is `shown` or `clicked`. The random 256-bit capability is scoped to one delivery, expires after 24 hours, and cannot read history, trigger a push, change settings, or access messages. It travels inside the encrypted push, never in a URL. No manager bearer is persisted in the service worker. Receipts are idempotent and stop a retry when browser receipt resolves an ambiguous provider outcome. A receipt network failure may leave display evidence unknown even when the browser displayed the alert.

## Message handling and account boundaries

- A follow must point to a room created by a configured managed account, stamped with its original mirror room, linked by that account in its configured space, and currently marked Share or Direct by that account. Private, unlinked, unowned or forged rooms are refused.
- The background reader uses the master manager's read authority. It cannot originate a native message, change Direct/Private settings, approve a conversation, or act as a source account.
- Eligible events are source-owner-authored text/media/location messages. Edits, state events, foreign senders and pre-follow timestamps are ignored. Incoming mode excludes `com.jkali.from_me: true`; all-message mode includes it.
- Native sending and receiving remain in the existing local user/bridge/uplink path. Once that path mirrors a message, the master notification service detects it. CRM does not introduce a separate notification/message path.
- Following begins at the latest forward cursor, so old history does not cause an initial notification storm. `(room,event)` hashes provide stable notification IDs. Cursor advancement, history creation and per-device outbox records commit together in SQLite.
- The worker polls every three seconds. One unavailable room does not block the other accounts. The UI polls status every 15 seconds and provides **Check for updates** for an immediate API sync.
- Explicit HTTP 429/5xx failures retry with exponential delay from five seconds, capped at five minutes and five attempts total. Other rejections fail immediately; 404/410 disables a dead subscription. A missing HTTP response is `unknown`, never disguised as HTTP 503 or automatically retried. Queued alerts ordinarily expire after one hour; explicit schedules/digests extend that lifetime as documented in the Telegram/list specification. Provider TTL remains five minutes. Notification tags affect visible replacement, not delivery evidence or deduplication.
- Unfollow, device disable and loss of sharing cancel pending work. A push already handed to the vendor cannot be recalled. No message body is sent to the push provider; the encrypted notification contains the conversation title and generic activity text.

## Persistence, deployment and maintenance

`<master-state>/notifications.local/` contains `notifications.db` and `vapid.pem`. The directory is mode 0700, files 0600, and the repository ignores it. Preserve both through deployments: the database retains follows/cursors/outbox/history/devices, and replacing the VAPID private key invalidates existing browser subscriptions. The updater now backs up the notification database with SQLite's online backup API and fingerprints/preserves the VAPID key. External-state installs project this directory with their other runtime data. For manual backups, stop enrollment or use SQLite's online backup API; do not copy an active SQLite database without consistency guarantees. Treat subscriptions and receipt capabilities as secrets.

The restricted Docker helper mounts only this directory, runs with the enrollment service UID/GID, drops capabilities and uses a read-only container filesystem. Its build context contains only the helper source, Dockerfile and pinned requirements, excluding repository credentials. Push endpoint validation permits only HTTPS on the supported Mozilla, Google and Apple push-service hostnames, validates browser key lengths, and refuses redirects. Docker networking is required for external push providers on this installation.

Rebuild the push helper and restart enrollment together when changing provider
error semantics. The first delivery-claim upgrade marks legacy pending retries
with prior attempts as `unknown`: older records cannot prove whether their retry
followed a rejection or a lost response. Subscriptions, VAPID keys, cursors,
notification IDs and accepted outcomes are preserved.

### Controller delivery adapter

Each `control_enqueue` entry contains exactly `room`, `event`, `kind` and `refs`.
For `kind: message`, refs contain `outgoing` and `rules: [{id,revision}]`.
For `kind: reminder`, refs contain `due`, `slot` and
`rules: [{id,revision,slot}]`, using UTC milliseconds. `uncontacted` is an allowed
reminder event identity; other events must be actual eligible source-owner messages
in a currently shared managed mirror. Entries cannot supply notification text or
override account identity. All entries validate before insertion. The endpoint
does not synchronously call back into the controller, preventing a circular wait;
normal delivery still rechecks canonical policy and current source sharing.

`control_outcomes` aggregates all destination devices. Acceptance on any device
wins; otherwise unknown/in-flight evidence prevents treating the channel as failed.
Pending work on an enabled device remains pending. `unavailable` means a recorded
notification has no runnable destination; a later subscription does not replay it.
`missing` means no local notification record was found, not proof that an earlier
ambiguous remote operation failed. Keep source-policy cancellation distinct from
provider failure. These APIs prepare centralized routing; they do not yet enable
channel preference/fallback by itself. The control service first calls
`control_dispatcher`, imports the frozen browser cursors into its durable queue,
then owns discovery for both destinations. The browser checks the handoff marker
under its queue lock before every legacy enqueue/cursor update, including a poll
whose network read began before handoff. It continues source/list validation,
delivery, receipts and reminder cancellation; it does not resume independent
discovery when the controller is offline. Start the updated controller after the
updated enrollment API, retaining both databases. Do not erase the handoff marker
or recreate queues as an outage workaround.

There is one narrowly scoped Trusted Types policy, `beepa-notification-worker`, which only accepts the exact notification-worker URL constructed by the module. It does not create TrustedHTML or general script capabilities. Keep the master HTML, `views/nginx.conf` master location and `master/gateway.py` CSP consistent. The user's app policy is unchanged. The worker acknowledgement endpoint is the only allowed non-GET operation in that worker's import-surface allowance.

To disable notifications, unsubscribe devices and unfollow rooms via API. Do not delete the state directory as a routine fix. Browser permission revocation may invalidate an endpoint later; reconnecting registers the current browser subscription again. Logging out unsubscribes the browser, while simply closing a tab preserves push delivery.

## Validation

- `tests/unit/master_notifications.test.py`: filtering, no backlog replay, dedup/restart, follow revocation, retries, device expiry, scoped receipts, idempotent tests, file permissions and endpoint validation.
- `tests/unit/master_notification_api.test.py`: manager-only HTTP actions, authentication before body parsing, limited receipt route and secret-safe errors.
- `tests/unit/master_notification_worker.test.js`: safe navigation, display acknowledgement only after success, click acknowledgement and denied-display behavior.
- `tests/integration/run.sh --notifications`: disposable real Synapse + actual HTTP API, manager/teammate authorization, real forward pagination, incoming/all modes, durable history, browser-receipt capability, restart and revocation. Only the external push vendor is injected.
- `tests/integration/master_notifications_browser.cjs`: actual master UI with fully intercepted synthetic services; follow/mode/test/history/exact-message navigation/disconnect and mobile layout.
- `tests/integration/master_notification_push_browser.cjs`: actual Chromium service worker under production CSP, actual Notifications API and HTTP receipt callback. A synthetic CDP push supplies the event; it is not live vendor/Mac banner evidence.
- `tests/integration/probe_notifications.py --i-am-sending-real-imessages --report /tmp/beepa-notification-live.json [--require-browser]`: explicit hardware probe sends one labeled message only to the configured self-chat through the local-user Matrix API, then waits for its real native incoming copy. It verifies the incoming copy on the user and master using the exact local timestamp carried by the uplink, correlates the exact master event ID to notification history, optionally requires browser display acknowledgement, and restores the prior follow setting. It exercises default incoming-only mode. This is a self-chat receive, not a claim of an independent person's reply.

Browser constraints: [permission requires a user gesture](https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API/Using_the_Notifications_API), and [showNotification resolves when the browser handles the display request](https://developer.mozilla.org/en-US/docs/Web/API/ServiceWorkerRegistration/showNotification). Mac banner visibility is a separate OS observation.

### Verification on this Mac, 2026-10-13

The live API test registered one browser and received its `shown_at` acknowledgement about 1.1 seconds after queueing a test alert. Repeating the same test request ID after restarting enrollment returned the same notification record without another send. The restart preserved the browser subscription and history.

The final hardware probe used incoming-only mode and one authorized self-chat message. The local user API accepted it, the native bridge reported engine acceptance after about two seconds, the actual native incoming self-copy reached the user and master, and the exact incoming master event produced a browser-acknowledged notification. Browser acknowledgement arrived about 0.5 seconds after that notification was recorded. The probe restored the prior follow preference. The local report is `/tmp/beepa-notification-live-final.json`; it contains test-only message/event identifiers, no credentials.

The full discovered unit/check runner, notification API/worker/UI checks, existing CRM browser regression, real isolated Synapse notification integration, update backup/WAL preservation and install portability checks passed. These results distinguish live browser display acknowledgement from OS banner visibility; Mac Focus may still suppress the visible banner. The notification-click route has automated coverage; a live click is recorded separately in `clicked_at` when used.
