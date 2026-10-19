# Tests and validation

`notification_routing.test.js` exercises durable routing decisions, independent
overlapping policies, exact-ID reconciliation, crash claims, deferral/revocation,
and fairness under unresolved attempts. `notification_routing_adapters.test.js`
uses the real Telegram outbox for rejection/fallback, bounded retries, accepted
and unknown outcomes, legacy/retired IDs, grouping and resolved-reminder
cancellation. Browser API responses are injected there; the separate Python
adapter and isolated notification integration tests cover actual browser enqueue
validation. `notification_dispatcher.test.js` exercises the actual monitor,
imported cursor drain, idempotent handoff and scheduled-digest timing. The isolated
Synapse notification integration covers authenticated handoff and real browser
fallback enqueue; Chromium control coverage verifies list preference, conversation
override and inheritance through canonical operations.

`control_discovery.test.js` verifies sync-based invalidation, full-state authority,
revoked sharing, unlinked rooms, account scope, failed sync/state reads, membership
changes and bounded negative caching. The real Synapse control test enables the
production cache and verifies an actual state-only update invalidates it, followed
by existing private-sharing/queued-summary revocation tests. Keep timeline event
types unfiltered in the sync request; limited timelines identify changed rooms,
they never provide the reconstructed authorization state.

Run `tests/run.sh` (or the configured host-runtime Python with `tests/run.py`).
The runner discovers every `tests/unit/*.test.py` and `*.test.js`, then runs
114,235 deterministic/fuzz consent vectors through both real resolvers and the
bridge-invite vectors through both real invite predicates
(`tests/conformance/invites_conformance.py`: `apps/user/invites.js` vs
`agents/uplink/invites.py`).
`--unit-only` omits conformance. Install the hash-locked `requirements-host.txt`
into a virtual environment when testing without an installed Beepa runtime.
The CI workflow pins Node/Python versions and records actual runtime versions.

## Live propagation probe (operator's own account only)

`python3 tests/integration/probe_propagation.py --i-am-sending-real-imessages`
sends ONE canary iMessage from the operator to their own handle (the
self-chat mapped in `imessage/state.db`) and times every hop with budgets
(daemon 30s, local echo 60s, master 90s). It addresses no other chat, changes
no setting, and prints only its own canary text. Exit 1 = a hop missed its
budget. Not part of `tests/run.py`; run it after any change to the iMessage
daemon, the uplink, or the Synapse stacks.

`--passive` sends nothing: it reads the hop stamps on the newest mirrored
events (median/max mirror lag), the uplink's health age and errors, and the
daemon's `/health`, exiting 1 when something is stale or retrying.
`tests/integration/launchd/com.jkali.beepa-probe.plist` runs it hourly and
appends to `agents/uplink/logs/probe.log` (install with
`launchctl bootstrap gui/$(id -u) tests/integration/launchd/com.jkali.beepa-probe.plist`).

## Disposable integration

`tests/integration/run.sh` creates BOTH local and master Synapse/PostgreSQL
stacks using unique project names, generated credentials, fixed-per-run allocated
loopback ports, a marked temporary state root, and synthetic accounts. It runs
the real uplink and cleans up only those stacks. It does not read live tokens,
provision the real master, send through real messaging bridges or touch real
Messages/Contacts data.

Commands:

```sh
tests/integration/run.sh                 # all sync scenarios
tests/integration/run.sh 3_offline        # filtered scenario
tests/integration/run.sh --enrollment    # one-time code/scoped account behavior
tests/integration/run.sh --roster        # additive/repeated setup retains accounts
tests/integration/run.sh --recovery      # scoped recovery and disposable database loss
tests/integration/run.sh --lifecycle     # full restore, control/browser state and stale-work quarantine
```

Directly running a scenario module without its generated `SYNCTEST_MANIFEST`
is refused. Never substitute production URLs/tokens/rosters. Do not use the
old hardcoded `matrix-synctest` compose fixture to run these suites; it is
historical and the runner generates all configuration itself.

## Behavior that tests must preserve

- Conversation authority is explicit Private/Share/Direct, with unknown values
  private. Contact sharing is separate. Match JS/Python resolver cases.
- Direct sends retain current manager/target/freshness/rate/identity gates.
  Stale proposals and ambiguous external sends must not be replayed as new sends.
- Scheduled sends are a SEPARATE gate and dispatcher, never a branch of the
  Direct ones. Keep: the fire-time re-read of the teammate's own event, the
  fail-closed cancellation point-read, the fire window (never a late silent
  fire), the attributed non-space target set, the shared rate counter, and the
  rule that losing the local queue cancels schedules instead of replaying them.
  The daemon must never WRITE a `com.jkali.scheduled_send`.
- A disconnected link suppresses forwarding even if environment credentials
  remain. Failed revocation cleanup remains durable and retryable.
- History/discovery gaps, re-share generations and master epochs have separate
  delivery state. Do not clear Direct outcomes when rebuilding an archive.
- iMessage unit tests use injected transports/fake CLI and temporary databases.
  Native executable updates, real grants, and self-send verification belong to
  explicitly authorized hardware tests, not unit or integration fixtures.
- Installer/update tests must retain credentials, configured identity, original
  volume names and the signed iMessage executable. Never fix a test by running
  setup or reset against this workspace's real account state.
- Master API changes require isolated real enrollment/scoping tests. Backup and
  restore tests may delete only a marked disposable test database.

Hardware acceptance on a second Mac and sustained load measurements remain
separate from passing an automated suite. See the implementation plan's gates.

## Master CRM browser test

`tests/integration/master_crm_browser.cjs` runs the actual master HTML/module
entry point under Chromium. It intercepts every request, serves allowlisted app
assets and synthetic Matrix responses, and refuses unknown requests. It never
uses the live browser profile, local session file or account credentials. With
Playwright Core and Chromium supplied by the test environment, run:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright-core CHROMIUM_PATH=/path/to/chromium \
  node tests/integration/master_crm_browser.cjs
```

For Alpine Chromium screenshots, install `font-dejavu` alongside `chromium` so
the receipt checkmark glyphs render. A missing font can leave valid receipt text
blank in a screenshot; inspect the actual image as well as DOM assertions.

`CRM_SCREENSHOT=/path/to/output.png` optionally captures the rendered list view.
This verifies list navigation, create/rename/archive/restore, shared-contact
selection across Elliot/David, explicit person linking, Direct preparation and
commit ordering, new-conversation approval state, reload, regular conversation
navigation and mobile layout. The independent `crm_members.test.js` uses the
real canonical store/reducer for cancellation, reassignment and concurrent-edit
cases. `crm_batches.test.js` similarly checks immutable review, staged proposals,
per-account child requests and recovery. These remain separate from real Synapse
source-dispatch scenarios and do not prove native provider receipts.

The browser fixture also walks through selecting recipients, personalizing text,
one central frozen review grouped by account, canonical parent persistence,
per-account queue/approval outcomes and idempotent reconciliation. The master
view fixture checks account/handle filters, hidden selection retention and
“Where we have them” navigation through the ordinary conversation viewer.
Cancellation tests assert saved parent intent before child requests, original
account identity after revocation, ambiguous replies, partial offline results
and refusal to resume a canceled batch. `master_import_surface.test.py` executes
the actual harness scanner against allowed and forbidden staging, submit and
cancel paths; typed writes remain confined to their named adapters.
The fixture now also projects exact synthetic read/reply references into the
real batch UI, opens both target message contexts in the existing viewer,
cancels remaining items and reloads the canonical cancellation. It verifies
that a reply alone leaves delivered checks gray and cancellation preserves
the evidence for an already attempted message. Every network request remains
intercepted; these UI checks do not replace source native-history tests.

## Notifications

`tests/integration/run.sh --notifications` exercises the real notification API
against disposable Synapse accounts with only the push vendor injected. The two
`master_notification*_browser.cjs` fixtures test the real UI and real Chromium
worker separately; neither is evidence of a Mac OS banner or live vendor delivery.
The worker test verifies nginx/HTML CSP parity. The opt-in
`probe_notifications.py` hardware probe requires explicit self-send authorization,
uses only the configured self-chat, correlates exact master events to notification
history and restores the original follow setting. Do not run it in routine CI.

## Telegram control coverage

`telegram_control.test.js` checks paired operator authorization, confirmation,
restart ambiguity, monitoring deduplication, original-time history and list
policy validation. `master_control_api.test.py` verifies the authenticated proxy.
`telegram_followups.test.js` checks ephemeral dialogue context, account choices,
permission changes, expiry, accepted-delivery gates, API session isolation and
clarification/confirmation separation. Hosted-model qualification uses synthetic
context only and is separate from deterministic tests; never send native messages
or real contact text as part of routine fixture evaluation.
`monitoring_schedule.test.js` exercises canonical schedule/override validation,
DST/overnight boundaries, quiet deferral, repeat collapse and independent overlap
policies. The matching Python test compares both evaluators over 1,800+ cases.
Keep notification timing checks on injected clocks; native messages are unnecessary.
`notification_groups.test.js` and `.test.py` cover collection windows, independent
overlap, exact digest routes, per-device receipt capabilities, grouping bounds,
revocation, backoff and restart ambiguity. Browser fixtures exercise the actual
cross-account digest picker and notification worker; no live provider/native send
is evidence from these fixtures. CDP activation telemetry must be awaited because
it can arrive after `navigator.serviceWorker.ready` resolves.
`run.sh --notifications` in the integration directory also exercises the real
headless domain against disposable Synapse through `telegram_control_client.js`.
`master_control_browser.cjs` runs the actual control/list components with all
network requests intercepted. Neither fixture can send native messages.
`conversation_native_evidence.test.py`, `conversation_tracking.test.py` and
`telegram_obligations.test.js` cover exact source ordering, bidirectional
message obligations, publication permission races, canonical per-message
deadlines/seen checkpoints and receipt/unread separation. Keep synthetic native
fixtures independent of installed accounts. The real Synapse control scenario
publishes synthetic source-owned tracking evidence and resolves an exact message
through the canonical journal; it does not invoke native sending.
`telegram_identity.test.js` preserves native outcomes across bot/token changes
and rejects old pairing/reference replay. `telegram_service.test.js` exercises
production HTTP authentication and independent CRM/Telegram recovery with injected
providers. The disposable Synapse scenario also serves canonical lists through
this runtime during a simulated Telegram outage; do not replace this with only
an assertion about the core class or mocked browser output.
`telegram_context_privacy.test.js` covers queued unquoted summaries, changes
between multipart deliveries, in-flight model revocation, restart, transient
verification outages, credential masking and exact replay. The disposable source
scenario revokes sharing after a summary is queued and verifies that its text is
removed without delivery. Keep native send outcomes and accepted-part evidence.
`telegram_async.test.js` holds the actual HTTP runtime's model call open and proves
that authenticated job submission/status and durable Telegram ingestion continue.
It verifies exact-ID retries, restart uncertainty, cancellation, operator/bot
isolation, retention and stable draft/delivery associations. Browser tests simulate
a lost submission response and require reuse of the same request ID, with separate
processing versus provider-acceptance labels.
`notification_adapter.test.py` exercises exact source-bound enqueue, duplicate and
malformed batches, multi-device evidence, concurrent claims, lost provider responses,
restart, late receipts and legacy retry migration. `notification_push_helper.test.py`
requires the optional push dependencies; when skipped on the host, run it inside
the built push-helper image with BEEPA_NOTIFICATION_HELPER=/app/notifications.py.
It injects the external provider and proves missing-response exceptions are not
converted into HTTP rejection codes. No real push is sent by those tests.
