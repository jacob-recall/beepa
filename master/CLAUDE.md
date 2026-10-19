# master/ — the always-on master stack, provisioning, and enrollment

`notification-routing.mjs` is the derived notification decision ledger;
`notification-routing-adapters.mjs` connects it to browser enqueue/outcome APIs
and the real Telegram outbox. The service calls the idempotent authenticated
`control_dispatcher` browser handoff before routing. Its frozen browser positions
are drained alongside existing Telegram cursors before being retired. Browser
discovery stays disabled after handoff, including across restarts; delivery and
reminder reconciliation continue. Do not start competing discovery loops.
Never infer failure from a missing
adapter response or replay an unknown attempt. Preserve existing channel IDs,
retired Telegram outcomes, and source-access/deadline validation at delivery.

Production `ControlDomain` enables incremental discovery. A fresh `/sync` barrier
invalidates changed room caches; only full `/state` reads supply authorization.
Initial sync requests a position without room payloads, then normal discovery
reads roster-authorized rooms. Later sync requests must not filter timeline types
or room IDs: that could hide state-only permission changes. Sync failures clear
all cache/token state and fail the refresh. Only explicit 403/404 refusals may be
cached negatively for <=60 seconds; a room delta invalidates those immediately.
Do not substitute cached discovery for live send/review validation or history.

The manager's durable, always-on Matrix homeserver: a second, completely
separate Synapse (compose project `matrix-master`) plus the scripts that
provision teammate accounts/spaces and issue enrollment credentials.
PLAN-MASTER-SYNC.md §6.2/§7/§9; PLAN-MASTER-SYNC-IMPL.md Phase 2/3.

**This is a separate local stack from the live `matrix-wa` hub
(`docker-compose.yml` at repo root, ports 8008/8009/8010). Never touch that
stack from here; never merge the two compose files.**

## What lives here

- `docker-compose.master.yml` — compose project `matrix-master`: Postgres
  (named volume, **no host port** — reachable only on this project's
  network) + Synapse (CS API only, bound to `127.0.0.1:8018`, healthcheck
  via `urllib` since the image ships no `curl`). `media_store` is a bind
  mount (`./synapse`, host-owned UID 501) rather than a named volume,
  specifically so the Synapse container (which runs as UID 501) can write
  uploaded media without a runtime `chmod` — a named-volume mountpoint
  would be root-owned and 500 on first upload.
- `setup.sh` — regenerates the **gitignored** `synapse/homeserver.yaml`
  from this **tracked** script: registration disabled, no federation,
  CS-API-only, and — importantly — loosened `rc_message`/`rc_room_creation`/
  `rc_invites` rate limits (a teammate's first big share creates many mirror
  rooms + events in one burst; the Synapse defaults would 429 partway
  through). Mints (or reuses, if already present) three shared secrets
  (`macaroon_secret_key`, `form_secret`, `registration_shared_secret`) into
  `synapse/.secrets.local` (600) and a signing key into
  `synapse/master.signing.key` (600, minted once via the Synapse image).
  Reads the Postgres password from `master/.env`
  (`MASTER_POSTGRES_PASSWORD`) so Synapse and Postgres agree. Safe to
  re-run: existing secrets/signing key are reused, so an existing DB keeps
  working.
- `provision.sh` — idempotent: provisions `@manager:master` and each roster
  teammate through `enroll.py provision-account` (registration via the
  shared secret + login with the configured password — see below — and a
  one-shot migration of any legacy stored password, `logout_devices:false`
  so existing sessions survive), then creates one Matrix **space** per
  teammate (`space:<user>`) owned by that teammate with `@manager` invited
  at `events_default: 50, users: {<teammate>: 100, manager: 0}` — read-only
  at the space level from the start. Writes `tokens.local` (600 — mxids +
  access tokens + space room ids) and `.provision-state.local` (600 —
  roster + space ids only; **no passwords, no secrets**).
- `enroll.py` — v1.5 enrollment-code issuance (replaces manual token
  handoff). `mint <teammate>` issues a short-lived, single-use code (only
  its SHA-256 is ever persisted, in `enrollments.local`, 600).
  `exchange <code>` redeems a valid/unused/unexpired code by
  **password-logging in as that teammate** to mint a fresh scoped token —
  so the token's scope is inherently Synapse-enforced, never something this
  script has to get right on its own — then burns the code (only *after*
  a token is successfully issued, so a transient master outage never wastes
  a code). `serve [--host] [--port]` exposes this as a loopback HTTP
  endpoint (`GET /enroll/health`, `POST /enroll/exchange`) for
  `agents/uplink/enroll_client.py` to call; in production it sits behind
  the same TLS reverse proxy as the CS API, so it adds no new public
  surface. Reads mxid/space/base-URL facts from `tokens.local` (600).

  **Passwords are derived by default.** Without an override, an account's
  password = `urlsafe_b64(HMAC-SHA256(TEAMMATE_PASSWORD_KEY, domain +
  localpart))[:32]`, key = the ASCII value in `synapse/.secrets.local`
  (written ONLY by `master/setup.sh`; `enroll.py`/`provision.sh` fail
  loudly if it is absent — nothing self-heals that file). Distinct HMAC
  domains for teammates vs the manager; localparts validated
  `[a-z0-9]{1,64}`, rejected never normalised. `enroll.py` is the single
  implementation — never re-implement the derivation anywhere. The key
  grants login-as-any-master-account authority (which the registration
  secret in the same file already implied); it also compromises accounts
  created *after* a read, so never copy it anywhere the registration
  secret is not (not `tokens.local`, not `.env`, not a launchd plist), and
  remember `synapse/` is bind-mounted into the Synapse container.
  `enroll.py password <lp> [--manager]` prints a live credential (the
  manager's console login): operator use only, never redirect it to a
  file, never add an HTTP route for it — the one sanctioned exception to
  the "no secrets on a script's stdout" rule below, because the
  alternative is storing it. `provision-account` (used by `provision.sh`)
  prints only `{mxid, token, migrated}`.
  **Custom manager password:** `synapse/.manager-password.local` may hold
  a non-empty JSON string (mode 600, gitignored). `account_password()` uses
  it for the manager only; provisioning, password lookup and restore all
  honor it. Teammate passwords remain derived. Setup leaves this file
  intact, and backups include it with `synapse/`. Provisioning can migrate
  the manager from the current derived password to the override while
  retaining existing sessions. A later custom-password change must also
  change the live account password; editing the file alone cannot migrate
  from an unknown previous custom password.
  **Key rotation:** `TEAMMATE_PASSWORD_KEY_PREV=<old>
  TEAMMATE_PASSWORD_KEY=<new> master/setup.sh` (for these two keys env
  wins over the file; value and set-ness are captured before the file is
  sourced) → `master/provision.sh` (each account logs in via `_PREV`,
  rotates with `logout_devices:false`, reports `migrated:true`) → once all
  report `migrated:false`, `TEAMMATE_PASSWORD_KEY_PREV= master/setup.sh`
  (explicitly empty = drop the `_PREV` line). Copies of `tokens.local`
  are copies of live bearer credentials — treat backups accordingly.
- `tailscale-serve.sh` — exposes THIS master node to teammates over Tailscale
  without opening any public/LAN port: `tailscale serve` puts a TLS-terminated
  ingress (real cert for the node's MagicDNS name) in front of the loopback
  Synapse (`https://<magicdns>` → `127.0.0.1:8018`) and enroll
  (`https://<magicdns>:8443` → `127.0.0.1:8019`), then records
  `MASTER_PUBLIC_URL` + `ENROLL_PUBLIC_URL` in `tokens.local`. `enroll.py`
  reads those (`_public_hs_url()` and the add-teammate `enroll_url`) so the
  credentials it hands teammates point at the tailnet, while it still talks to
  Synapse locally via `_cs_base()` (`127.0.0.1:8018`). Idempotent; re-run after
  a reboot. This is the "TLS reverse proxy" the loopback-bind notes assume.
- `synapse/` — the rendered (gitignored) Synapse config + signing key +
  secrets + media store. Regenerated by `setup.sh`; never edit
  `homeserver.yaml` by hand or commit anything under here.
- `tokens.local`, `.env`, `.provision-state.local` — gitignored, mode 600,
  never commit.

## Security invariants (do not weaken)

- **Registration disabled; only the CS API is exposed.** `enable_registration:
  false` in the rendered `homeserver.yaml`; the only way to create an
  account is `register_new_matrix_user` via the shared secret
  (`provision.sh`), never the public registration endpoint. Federation
  stays off (`federation_domain_whitelist: []`).
- **Per-teammate scoped tokens.** Each teammate's token comes from logging
  in *as that teammate* — Synapse itself enforces that `@alice`'s token
  cannot write `@bob`'s rooms. Never create or distribute a token that
  spans multiple teammates (e.g. an admin token used in place of a
  per-teammate one for the uplink).
- **The manager is read-only from the moment a space/mirror room is
  created**, not read-only by later policy: `provision.sh`'s space creation
  and `agents/uplink/uplink.py`'s `create_mirror` both pin
  `@manager:master` to power level 0 with `events_default` above it in the
  same `createRoom` call. Any new room-creation path (a new kind of
  per-teammate room) must set an equivalent override at creation time, not
  rely on a follow-up power-level change.
- **No host port on Postgres.** It must stay reachable only on the
  `matrix-master` compose network — never add a host port mapping for it.
- **Secrets stay 600, and stay out of git.** `synapse/.secrets.local`,
  `synapse/master.signing.key`, `master/.env`, `master/tokens.local`,
  `master/.provision-state.local`, `master/enrollments.local` are all
  gitignored and written mode 600 by the scripts that produce them. Don't
  add a path here to a script's stdout/log, and don't relax a `chmod`.
- **Enrollment codes are single-use, short-lived, and stored hashed.**
  `enroll.py` never persists a code's plaintext, only its SHA-256; a code
  is marked used only after successful token issuance; an expired/used/
  invalid code is refused (403). The `serve` endpoint binds to loopback —
  it relies on the surrounding TLS reverse proxy for any real-network
  exposure, the same as the CS API.
- **This stack never touches the live `matrix-wa` hub.** Different compose
  project name, different ports (8018 vs. 8008/8009/8010), different
  volumes. A `docker compose -p matrix-master ... down -v` only affects
  `matrix-master`'s own volumes; it is safe. **Never** run `down -v`
  against the `matrix-wa` project.

## How to run / test

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"

# 1. render config + mint secrets (safe to re-run):
master/setup.sh

# 2. bring the stack up:
docker compose -p matrix-master --env-file master/.env \
  -f master/docker-compose.master.yml up -d

# 3. provision accounts + spaces (idempotent):
master/provision.sh
# -> writes master/tokens.local (600) with every mxid/token/space id
#    the uplink and the integration harness need.

# 4. (optional, v1.5) mint + redeem an enrollment code instead of copying
#    tokens.local by hand:
python3 master/enroll.py mint alice
python3 master/enroll.py serve --port 8019 &   # loopback exchange endpoint
python3 agents/uplink/enroll_client.py --enroll-url http://127.0.0.1:8019 \
  --code <CODE> --out ./uplink.env.local

# tear down (matrix-master ONLY — never matrix-wa):
docker compose -p matrix-master -f master/docker-compose.master.yml down -v
```

`tests/integration/test_enroll.py` and `tests/integration/harness.py` both
require this stack up + provisioned first — see `tests/CLAUDE.md`.

## How to change this safely

### CRM metadata bootstrap (implementation in progress)

`POST /admin/crm-room` authenticates the configured manager before reading the
request body and accepts only an empty object. `crm.py` resolves the fixed
`#beepa-crm-v1:<server>` alias, creates a private non-federating version-11 room
only when that alias is absent, and verifies creator, schema, membership and
permissions before returning its ID. A timeout or alias collision re-resolves
the same alias; a mismatched existing room is refused, never repaired or replaced.
The caller token is used only against this master homeserver and is not returned.

The manager owns the metadata room. Its normal timeline write is
`com.jkali.crm.operation`; ordinary messages, encrypted messages, unknown event
types and invitations are denied by the initial power levels. The manager may
administer its own room policy, so this is an accidental-use guard, not a claim
that its own room can never be changed. Native-conversation authority remains
in the existing per-user proposal and uplink boundaries. This endpoint neither
starts conversations nor changes Direct permissions. Schema operations and
client replay live in `shared/model/crm.js` and `apps/master/crm-store.js`.

Unit coverage is `tests/unit/master_crm_room.test.py`; real isolated Synapse
coverage is scenario `28_crm_metadata`, including restart/replay and concurrent
edits through the actual JavaScript store. No production provisioning is needed
to run those tests.

1. Any change to `docker-compose.master.yml` or `setup.sh`'s rendered
   `homeserver.yaml` is security-sensitive (isolation/hardening, per
   PLAN-MASTER-SYNC-IMPL.md P2.1) — keep registration disabled, federation
   off, CS-API-only, Postgres host-port-free.
2. If you add a new provisioned room type (beyond spaces and mirror/
   proposals rooms), give the manager an explicit power-level override at
   creation, following the existing pattern — never rely on a default.
3. Never commit anything under `synapse/`, `tokens.local`,
   `.provision-state.local`, `.env`, or `enrollments.local` — they're
   gitignored for a reason; if you add a new secret-bearing output file,
   gitignore it and chmod it 600 in the script that writes it.
4. Re-run `tests/integration/test_enroll.py` after any change to
   `enroll.py`'s exchange/expiry/reuse logic, and the full
   `tests/integration/harness.py` suite after any change to provisioning
   (account creation, space power levels) since most scenarios depend on
   the exact power-level shape provisioned here.

### Master browser notifications

`notifications.py` provides durable follows, mirror polling, an outbox and delivery
evidence through manager-only `POST /admin/notifications`. `notify.py` is the API
CLI. `POST /admin/notifications/receipt` accepts only a one-delivery random
capability to acknowledge browser display/click; no manager bearer enters a worker.
The push helper is built by `setup-notifications.sh`; preserve the private
`notifications.local/` directory and VAPID key. This component reads mirrors only
and must never acquire native send authority. See
`docs/specs/2026-10-13-master-notifications.md` for the API and deployment contract.

## Telegram control (2026-10-17)

`control-core.mjs`, `control-domain.mjs` and `telegram-service.mjs` implement the
paired Telegram manager interface. `setup-control.py` installs its restricted
loopback Docker worker. Keep `control.local/` private and durable. Canonical list
operations own monitoring policies; SQLite holds operational delivery state.
The manager-only `/admin/control` proxy must authenticate before parsing.
Enrollment mutations stay serialized even though control callbacks require a
threaded HTTP server. Every native send stays in the existing source dispatcher;
the worker can only submit typed proposals after an exact operator review.
See `docs/specs/2026-10-17-telegram-master-control.md` for setup, APIs and tests.
`lifecycle.py` and `control_recovery.py` back up the exact private control/browser
files using SQLite snapshots after stopping their writers. Preserve newer local
operational evidence on restore; quarantine old pending/uncertain work and require
fresh transport pairing when only a snapshot survives. Refresh manager credentials
after auth sanitization. Never copy live WAL sidecars or replay a restored queue.
Worker maintenance must verify both immutable container ID and the selected state
mount before stopping/starting it. Recovery tests use only marked disposable stacks.
Waiting and reminders now require source-owned `com.jkali.conversation_tracking`
with matching account generation and exact mirrored message IDs. Never fall
back to the latest mirror arrival to claim an unanswered message. Both channels
recheck per-message deadlines, snooze, answers and policy revisions before
delivery. Master-seen checkpoints are canonical list metadata, not native reads.
Health components are independent; do not let monitoring clear Telegram/model
errors. Pending reviews are revalidated before every outgoing review part and
during monitoring. Invalid reviews retain an ID/outcome tombstone but lose their
cached bodies. Previously submitted/uncertain work retains reconciliation data;
never erase it or retry a send as part of review cleanup.
Basic access/retention settings are canonical `control.settings` operations.
Maintenance clears aged cached content while retaining deduplication tombstones
and immutable uncertain/batch-submission payloads. Never interpret cache pruning
as permission to retry a native send, nor promise deletion of provider/backups.
Dialogue context is a bounded in-memory cache, never a second source of access
or confirmation authority. Revalidate its discovery fingerprint before model
use, require accepted delivery for remembered exchanges, separate API session
IDs from Telegram, and discard on disconnect, expiry or model-access disable.
An ordinary clarification must never make bare yes confirm an unrelated draft.
Telegram and browser push use the same canonical list/watch schedules and repeat
settings. Preserve default any-time delivery and no implicit reminders/repeats.
Recheck independent list eligibility at delivery; a quiet/removed list must not
suppress another valid list or grant previews/repeats to it. Telegram references
stay ID-only; optional message previews are re-read before delivery. Browser
queued records retain list revisions and re-read canonical policy on retries.
Canonical `presentation` controls activity grouping/digests for both channels.
Keep per-event jobs as the dedup ledger, atomically claim group members, and never
retry uncertain in-flight groups after restart. Browser group receipt capabilities
bind a frozen set of event jobs to one device; they grant no source authority.
Multi-conversation Telegram quotes require an exact numbered/button choice before
reading context. Every route is rechecked against current source sharing.
Domain startup and monitoring must remain independent of Telegram availability.
Only a verified numeric bot ID may retain a pairing across token rotation. A bot
change retires transport-only queues/references and requires fresh pairing; keep
per-bot input/outcome tombstones and never reset native/API deduplication or source
watermarks. An unbound legacy database cannot silently adopt an existing pairing.
Ordinary queued model answers need the same revocation protection as reviews:
refresh and validate their durable source-scope/history hashes before every part,
retire stale cached text/references without losing outcomes, and defer on transient
verification failures. Recheck scope across hosted rounds. Mask recognized
credentials before truncation/hosted processing and dialogue caching; literal
authorized message replay stays exact and cannot be silently rewritten.
API commands are durable jobs: acknowledge before execution, bind request IDs to
exact text/session hashes, and keep processing separate from reply/native delivery.
Fast authenticated submission/status/queued cancellation must not wait for the
model/domain queue. Telegram ingestion commits before its poll offset advances and
does not wait for command execution. One serialized consumer executes work; a
crashed processing job is unknown and never replayed. Keep request/draft/outcome
tombstones across bot rotation while erasing completed input text.
Release the domain execution lease only for a hosted-model wait; reacquire it
before validating response scope or executing any tool. The command consumer
remains single. Access/identity changes while suspended invalidate its answer;
disconnect/re-pair to the same operator must not reuse the old connection epoch.
Do not let an old bot's suspended input finish a new bot's reused update ID.
Control database paths must be absolute within an explicit private state directory;
reject malformed/relative paths before mkdir/chmod/SQLite can affect the workspace.
Browser single/group pushes claim durable work before the external call. Preserve
unknown outcomes after timeouts/crashes, bounded retries only for explicit HTTP
rejections, and late scoped receipt reconciliation. The controller adapter's
enqueue/outcome calls must never synchronously call back to the waiting controller.
Adapter enqueue verifies source identity but cannot change list policy or supply
message text. The central dispatcher takes ownership through its durable handoff marker;
never restart legacy discovery after that handoff. Contact reminders use typed
account/binding/generation/episode identities with no fabricated room. Browser
enqueue never calls the controller back; delivery revalidates current contact
access, canonical task, source freshness, policy revision and reminder slot.
