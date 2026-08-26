# Plan: Local WhatsApp↔Matrix bridge (wa-bridge-local-v3)

Approved design: self-hosted mautrix-whatsapp on this Mac, localhost-only, via
Docker Compose. v2 fixed image/locale/acceptance/rollback blockers; v3 adds a
provable Postgres-backend check (S1), a user→bot command round trip (S3), and
the completed security review with dispositions (below).

## Outcome
A Docker Compose stack in `/Users/jkali/work/pm_mng` running:
- **Synapse** homeserver (`server_name: localhost`) on `127.0.0.1:8008`
- **PostgreSQL 16** with two databases (`synapse`, `mautrix_whatsapp`)
- **mautrix-whatsapp** bridge, registered with Synapse as an appservice
- **Element Web** on `127.0.0.1:8009`, preconfigured to this homeserver

End state: `@jkali:localhost` can log in AND has exercised a command round trip
with `@whatsappbot:localhost` (bot replied, no permission denial); user then
runs `login qr` themselves (needs their phone — out of scope for automation).

## Non-goals
Federation, TLS/domains, exposure beyond 127.0.0.1, end-to-bridge encryption,
phone/mobile client access, VPS deployment, automated backups.

## Images (digest-pinned; multi-arch/arm64; digests recorded at first pull)
- `ghcr.io/element-hq/synapse:v1.159.0@sha256:edf259d2b575b669a3e81024918ab8d5cfb7d2fba5a53c9e09695f1abc5645cb`
- `postgres:16.10-alpine@sha256:029660641a0cfc575b14f336ba448fb8a75fd595d42e1fa316b9fb4378742297`
- `dock.mau.dev/mautrix/whatsapp:v26.08@sha256:86237c4d0d33a1e08910b1f820e6c561f9b8e21dc26943caf266e01021087002`
- `vectorim/element-web:v1.12.26@sha256:a9be04cef41ed94cba0dcaeca5a81f89826d4f8be3d57ba5a6fa988f34eda703`

Digest pinning trades away silent patching: final report must tell the user to
bump versions periodically (a stale bridge breaks when WhatsApp changes protocol).

## Constraints (security) — informed by security review below
- Only two published ports, both with the literal `127.0.0.1:` prefix in
  `ports:`: `127.0.0.1:8008` (Synapse), `127.0.0.1:8009` (Element). Postgres
  and the bridge (appservice `http://mautrix-whatsapp:29318`) publish no host
  ports. Proof by listener enumeration (`docker compose ps` +
  `lsof -nP -iTCP -sTCP:LISTEN`): no `0.0.0.0` and no `[::]` binding for stack
  ports, plus grep of the compose file for the `127.0.0.1:` prefix.
- Public registration disabled and *proven*: `POST /_matrix/client/v3/register`
  returns `M_FORBIDDEN`. `report_stats: false`.
- `registration_shared_secret` is **removed** from `homeserver.yaml` (and
  Synapse restarted) after the single account is created in S2.
- Containers run with the host user's UID/GID (Synapse `UID=501,GID=20`;
  mautrix image honors the same envs) so bind-mounted configs can be `600`.
  Project dir `700`; `homeserver.yaml`, signing key, `registration.yaml`,
  bridge `config.yaml`, compose file, `.env` all `600` (no group/other read).
- Matrix password: generated, passed via `--password-file` (0600 temp file,
  deleted after), never in argv/history; delivered to the user once in the
  final message only.
- Postgres: password auth only — never `POSTGRES_HOST_AUTH_METHOD=trust`;
  S1 asserts a passwordless connect from another container fails.
- Bridge config: `permissions` map contains exactly one entry
  (`"@jkali:localhost": admin` — no `*`, no domain entry); provisioning API
  disabled (`provisioning.shared_secret: disable`).
- Element `config.json`: points only at `http://localhost:8008`; no identity
  server, no integration manager URLs, `disable_guests: true`, no analytics.
- `.gitignore` created at scaffold covering all generated configs, data dirs,
  and `.env` (guards a future `git init`).
- FileVault: verified **On** (2026-08-25) — at-rest protection for the
  WhatsApp session keys and message archive in the Docker VM disk.

## Slices
- **S1 — Scaffold + core**: `.gitignore` + compose file + dirs; Postgres init
  script creating both DBs with `ENCODING 'UTF8' LC_COLLATE='C' LC_CTYPE='C'
  TEMPLATE template0` (init runs only on an empty Postgres volume; re-init
  requires volume removal — see Rollback). Generate Synapse config, configure
  Postgres backend, start postgres+synapse.
  *Acceptance*: (a) `curl http://127.0.0.1:8008/_matrix/client/versions`
  returns JSON; (b) `pg_database` shows `C`/`C`/`UTF8` for both DBs;
  (c) **Postgres actually in use**: `homeserver.yaml` `database.name: psycopg2`
  AND the `synapse` DB contains Synapse's schema (non-zero table count /
  `schema_version` present) after startup; (d) Synapse log free of
  collation/ctype errors; (e) listener enumeration: only `127.0.0.1:8008`
  published, no `[::]`; (f) `docker compose images` shows the pinned digests;
  (g) register endpoint returns `M_FORBIDDEN`; (h) passwordless psql connect
  from another container fails.
- **S2 — User account**: create `jkali` with generated password via
  `register_new_matrix_user --password-file`; verify
  `POST /_matrix/client/v3/login` succeeds; then remove
  `registration_shared_secret` from `homeserver.yaml`, restart Synapse,
  re-check acceptance (a).
  *Acceptance*: login returns an access token (kept only in-memory for S3's
  round trip, then discarded); secret line absent from config; Synapse healthy.
- **S3 — Bridge**: back up `homeserver.yaml` first. Generate bridge
  `config.yaml` (homeserver `http://synapse:8008`, domain `localhost`,
  postgres URI, appservice address `http://mautrix-whatsapp:29318`,
  single-entry permissions, provisioning disabled, backfill left at generated
  defaults — disposition F10), generate `registration.yaml`, add to Synapse
  `app_service_config_files`, restart Synapse, start bridge.
  *Acceptance (positive, bidirectional, authorization-proving)*:
  (a) `GET /_matrix/client/v3/profile/@whatsappbot:localhost` returns 200;
  (b) bridge log shows a successful Synapse-initiated ping/transaction;
  (c) **command round trip**: using S2's token, create a DM with
  `@whatsappbot:localhost`, send `ping` (or `help`), receive a bot reply event
  that is not a permission denial; (d) `permissions` map has exactly one entry.
  *Slice rollback (preserves S1/S2)*: restore backed-up `homeserver.yaml`,
  remove registration file and bridge container, restart Synapse; re-check
  S1(a) and S2 login still pass.
- **S4 — Element Web**: minimal `config.json` per constraints, container on
  `127.0.0.1:8009` only.
  *Acceptance*: HTTP 200 on `/`; served config has this homeserver only, no
  integration/identity/analytics endpoints; listener check for 8009.
- **S5 — Verification**: fresh `pilotfish:verifier` on the exact claim:
  "S1–S4 acceptance all pass: pinned-digest stack healthy on Postgres, login
  works, registration forbidden, bridge bot answered a `ping` from
  @jkali:localhost, only 127.0.0.1:8008/8009 published (no `[::]`), no stack
  file group/other-readable" before handoff for `login qr`.

## Security review (pilotfish:security-reviewer, 2026-08-25) — dispositions
No P0/P1 findings. Framing: any process running as this user owns the Docker
socket and thus the stack; file hygiene guards casual reads, while the two
load-bearing controls are FileVault (verified On) and loopback-only binding.

| ID | Finding | Disposition |
|----|---------|-------------|
| F1 | registration_shared_secret retained → local admin-mint | **Mitigate**: removed after S2 (constraint above) |
| F2 | as/hs tokens world-readable, UID mismatch traps | **Mitigate**: 700/600 perms + UID=501/GID=20 (S1/S5 check) |
| F3 | WhatsApp session keys plaintext in Postgres; theft = live session | **Mitigate**: FileVault verified On; report names phone-side unlink as kill switch; keep dir out of unencrypted cloud sync |
| F4 | password in argv/history | **Mitigate**: `--password-file`, deliver once |
| F5 | DB creds in configs; `trust` trap | **Accept** (dominated by socket access); guard: no trust auth, S1(h) proof |
| F6 | plaintext HTTP, CORS `*`, origin-squat on 8009 | **Accept** (loopback TLS buys nothing); registration-off + strong password load-bearing → proven S1(g) |
| F7 | one missing `127.0.0.1:` string exposes to LAN | **Accept design, keep proof**: listener + compose-literal checks incl. `[::]` |
| F8 | in-network exposure of appservice + provisioning API | **Accept** exposure; **mitigate** sprawl: provisioning disabled; no unrelated containers on this network |
| F9 | wildcard default in bridge permissions | **Mitigate**: exactly one entry, checked in S3(d) |
| F10 | permanent plaintext archive incl. ephemeral msgs; backfill scope | **Accept** E2BE-off (theatre on single box); backfill at generated defaults, stated plainly in final report |
| F11 | ToS/ban risk; session=bearer credential; destructive `down -v` | **Accept** 1–2 with disclosure in report; **mitigate** 3: runbook warns `down -v` destroys WhatsApp session+history post-link; pg_dump before destructive ops |
| F12 | Element stock third-party endpoints (scalar/vector.im/PostHog) | **Mitigate**: minimal config.json + `report_stats: false` |
| F13 | mutable tags, third-party registry | **Mitigate**: digest pins (above) + update-cadence note in report |
| F14 | registration-off asserted, unproven | **Mitigate**: S1(g)/S5 `M_FORBIDDEN` check |
| F15 | future `git init` leaks secrets | **Mitigate**: `.gitignore` at scaffold |
| F16 | `server_name: localhost` permanent | **Accept**, disclosed in report (migration = start over) |

## Rollback
- S3 has the slice-local rollback above.
- Full teardown: `docker compose down -v` + delete generated files.
  **Warning (F11)**: after a successful WhatsApp link this destroys the
  session and all bridged history — `logout` via bot or unlink in WhatsApp →
  Linked Devices first, and `pg_dump` anything worth keeping.

## Stops
Any slice failing after 2 distinct fix attempts → stop and report.
WhatsApp QR login is user-owned; the task ends at a verified ready-to-scan state.
