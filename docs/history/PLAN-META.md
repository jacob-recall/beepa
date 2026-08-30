> ARCHIVED (2026-08-30): historical planning doc, superseded — kept for reference only.

# Plan: Instagram bridge via mautrix-meta (meta-bridge-local-v1)

Approved design (2026-08-26). Adds a 4th bridge — **mautrix-meta in Instagram
mode** — to the existing localhost-only Matrix hub, following the exact pattern
established for mautrix-whatsapp (see PLAN.md) and iMessage. Instagram DMs only;
Facebook/Messenger explicitly out of scope for v1.

Parent planner/integrator: Fable (main session). Execution via pilotfish roles.
Isolation: a `git worktree` at `.claude/worktrees/meta-bridge` on branch
`feat/meta-bridge` (mirrors the concurrent `feat/linkedin-bridge` worktree).
Work tracked in beads (epic `pm_mng-r8e`). This plan is deliberately
**non-overlapping** with the other agent (LinkedIn bridge + hub Home feed): I
own bridge infrastructure and hand off the hub's Instagram source as a written
spec (S4), never live-editing `hub/site/app.js`.

## Outcome
A `mautrix-meta` appservice running in the `matrix-wa` compose project:
- Bot `@instagrambot:localhost`, registered with Synapse as an appservice.
- Own Postgres DB `mautrix_meta` (C/C/UTF8, owner `matrix`).
- Config `network.mode: instagram`, single-entry permissions, provisioning off,
  backfill **off** (anti-flag posture), gentle contact sync.
- `@jkali:localhost` can `login instagram` and complete a `ping`/`help` command
  round trip (authorization-proving). Actual cookie login is user-owned — the
  task ends at a verified ready-to-login state.

## Non-goals
Facebook/Messenger networks, federation, TLS/domains, exposure beyond
127.0.0.1, end-to-bridge encryption, mobile access, VPS deployment, browser
automation of the Instagram login itself, follower-list scraping, editing
`hub/site/app.js` (handed off as a spec).

## Login & anti-ban posture (the "don't get caught" requirement)
Chosen approach: **user logs in, I only guide.**
- The user authenticates on instagram.com in their **own everyday Chrome**
  (residential IP, real browser fingerprint), with **2FA enabled**.
- Then `login instagram` to `@instagrambot:localhost` and paste the "Copy as
  cURL" of a `graphql` XHR (or the cookies `sessionid, csrftoken, mid, ig_did,
  ds_user_id` as a JSON object).
- No automation of login keystrokes — automated input on IG's login page is the
  canonical bot-detection trigger.
- Structural safeguards (single personal account):
  - Runs on **localhost / the user's residential IP** (already true) — no
    datacenter IP, the single biggest flag for a bridged session.
  - **backfill / history sync OFF** — aggressive history pulls are a top
    behavioral flag; new messages bridge from login onward.
  - **Gentle contact sync** — bridge-native participant sync only, no bulk
    follower/following enumeration.
  - Reconnect/refresh intervals left at conservative bridge defaults.
- Kill switch (README): Instagram → Settings → Accounts Center → Password &
  security → Where you're logged in → remove the bridge device. Invalidates the
  session cookies stored locally.

## Contacts (sync + reuse Directory)
- Enable the bridge's own contact / DM-participant sync so Instagram contacts
  appear as Matrix ghosts (throttled; no mass scrape).
- Surface them through the hub's **existing** Directory + start-chat surface
  (PLAN-IMSG-STARTCHAT) — delivered as part of the S4 handoff spec, not a new
  Instagram-specific UI.

## Image (digest-pinned)
- `dock.mau.dev/mautrix/meta:latest@sha256:61d684f9e385c6150cabe1a49555a409a97a154f2466b45626298de51f1c3f40`
  (build `v26.08+dev`, arm64 multi-arch index; pinned 2026-08-26). Mirrors the
  digest-pinning discipline in PLAN.md.

## Deviation (S2, accepted by integrator; flagged for user sign-off)
Invariant "`network.mode: instagram`" is **not implementable** in this image:
the field does not exist (absent from `-e` output; no `Mode` in the connector
`Config` struct; the key is stripped on config-save). Current mautrix-meta is a
**unified** bridge — the network is chosen at **login time** via `login
instagram`. "Instagram-only" is therefore enforced by (a) the single-entry
admin permission (`@jkali:localhost` only) and (b) the user running only
`login instagram`, not by a config toggle. No new external surface (loopback-
only, single-user). Accepted as a spec-vs-image reality; the README warns the
user to run only `login instagram`.

## Constraints (security — inherits PLAN.md posture)
- **No host ports** for the bridge: appservice reachable only on the compose
  network. Proof by listener enumeration (no new `0.0.0.0`/`[::]` binding).
- New secret-bearing state lives under `meta/` — **git-ignored** (added to
  `.gitignore` before any commit). config.yaml (`600`), registration.yaml
  (`600`), `meta/` dir `700`; UID/GID 501/20 like the other services.
- `permissions` map: exactly one entry `"@jkali:localhost": admin` — no `*`, no
  domain entry. Provisioning API disabled.
- Homeserver `http://synapse:8008`, domain `localhost`; DB via in-network
  Postgres URI (password auth, never `trust`).
- Synapse `app_service_config_files` gains `/data/meta-registration.yaml`
  (this file lives under gitignored `synapse/`, edited only in the live main
  tree; PLAN-HOME/home-v1 does not touch it → no race).
- Session cookies (the Instagram bearer credential) are stored by the bridge in
  `mautrix_meta` / `meta/` — at-rest control is FileVault On (same as WhatsApp).
  Never logged, never committed.

## Slices
- **S1 — Infra scaffold** (main, as integrator into the shared runtime):
  confirm + pin the meta image digest from the live registry; add the
  `mautrix-meta` service to `docker-compose.yml` (profile `bridge`, no ports,
  UID/GID, `./meta:/data`, depends_on postgres healthy + synapse started);
  create `mautrix_meta` DB on the running Postgres via one-shot `createdb`/psql
  (C/C/UTF8, owner matrix) AND append its `CREATE DATABASE` to
  `postgres-init/01-create-bridge-db.sh` for clean rebuilds.
- **S2 — Bridge config + registration** (`pilotfish:security-executor`):
  generate `meta/config.yaml` (`-e`), set `network.mode: instagram`, homeserver
  + domain, Postgres URI, appservice id/tokens + address
  `http://mautrix-meta:29319` (distinct from whatsapp's 29318), single-entry
  permissions, provisioning disabled, **backfill off**, gentle contact sync;
  generate `meta/registration.yaml` (`-g`); back up `homeserver.yaml`, add
  `/data/meta-registration.yaml`, restart Synapse, start the bridge; set perms.
- **S3 — Login runbook + anti-ban** (main + user): cookie-extraction runbook
  (guide only), confirm ready-to-login, document kill switch/posture in README.
- **S4 — Hub integration spec** (main): `SPEC-META-HUB.md` + beads handoff
  issue (`pm_mng-ebn`) for the other agent to add Instagram as a hub SOURCE —
  space identity, CSP-safe badge, Directory + start-chat + Home-feed wiring,
  honoring PLAN-HOME H-1..H-7 / HF-1..HF-9. **I do not edit `hub/site/app.js`.**

## Acceptance (S1+S2, verified by fresh pilotfish:verifier)
Meta bridge healthy on Postgres; `@instagrambot:localhost` answered `help`/`ping`
from `@jkali:localhost` (not a permission denial); `network.mode: instagram`;
backfill off; permissions has exactly one entry; only 127.0.0.1 ports published
(no new listener, no `[::]`); `meta/` untrackable by git; image digest-pinned;
WhatsApp + iMessage bridges unaffected.

## Rollback
- S2 slice-local: **surgically remove only** the `- /data/meta-registration.yaml`
  line from `synapse/homeserver.yaml` (do NOT restore the `.backup` wholesale — a
  concurrent gmessages agent edited the file after the backup was taken; a blind
  restore would clobber its `gmessages-registration.yaml` line). Then
  `docker compose stop mautrix-meta && docker compose rm -f mautrix-meta`,
  `docker compose restart synapse`; re-check other bridges + `@jkali` login.
- Full teardown of just this bridge: `logout` via the bot first (invalidates IG
  session), stop+remove `mautrix-meta`, drop `mautrix_meta` DB, remove the
  registration line, restart Synapse, delete `meta/`.
- **Never** `docker compose down -v` after login without `logout` first.

## Coordination / Stops
- Source changes committed on `feat/meta-bridge`; runtime deploy (ignored
  `meta/` + `synapse/` edits + `docker compose up -d mautrix-meta`) happens in
  the shared main tree, additively. The one shared disturbance is a ~10s Synapse
  restart to load the registration; bridges/clients auto-reconnect.
- Any slice failing after 2 distinct fix attempts → stop and report.
- Instagram cookie login is user-owned; task ends at ready-to-login + S4 spec.
