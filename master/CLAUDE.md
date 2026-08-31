# master/ — the always-on master stack, provisioning, and enrollment

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
  shared secret + login with the **derived** password — see below — and a
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

  **Passwords are derived, never stored.** Every master-side account
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
  **Key rotation:** `TEAMMATE_PASSWORD_KEY_PREV=<old>
  TEAMMATE_PASSWORD_KEY=<new> master/setup.sh` (for these two keys env
  wins over the file; value and set-ness are captured before the file is
  sourced) → `master/provision.sh` (each account logs in via `_PREV`,
  rotates with `logout_devices:false`, reports `migrated:true`) → once all
  report `migrated:false`, `TEAMMATE_PASSWORD_KEY_PREV= master/setup.sh`
  (explicitly empty = drop the `_PREV` line). Copies of `tokens.local`
  are copies of live bearer credentials — treat backups accordingly.
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
