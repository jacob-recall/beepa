# Architecture — what actually exists and runs

The inventory below is a historical snapshot from 2026-08-28. Current authority and lifecycle behavior are described in [SYSTEM-DESIGN.md](SYSTEM-DESIGN.md) and [UPDATES.md](UPDATES.md). Components added during the September reliability work are covered by the implementation plan.

At the time of the original audit, everything in this document was
verified against the code or the live system on that date unless explicitly
marked **unverified**. Where this document disagrees with `README.md` history,
`PLAN*.md`, or a `CLAUDE.md`, this document reflects observed reality; see
`docs/AUDIT-FINDINGS.md` for each discrepancy.

Companion docs: `docs/SYSTEM-DESIGN.md` (the data-schema explainer),
`docs/AUDIT-FINDINGS.md` (defects), `docs/SIMPLIFICATION-PLAN.md` (cleanup
proposals).

## What this system is

**Beepa** (repo `jacob-recall/beepa`, working dir `pm_mng`) is a self-hosted,
local messaging hub with a private Tailscale master layer:

1. **A personal hub** that pulls one person's chats from six networks
   (WhatsApp, iMessage, Google Messages, Instagram, LinkedIn, X/Twitter) into a
   private Matrix homeserver on their own machine, with a no-build web UI.
2. **A master-sync layer** that lets a teammate *opt in* to mirroring selected
   conversations, read-only, to an always-on "master" homeserver, where a
   manager can read and propose replies. Explicit Direct conversations permit guarded automatic sending by the teammate uplink.

Everything is plain files: static ES-module web apps (no bundler, no npm),
Python host services, a small pinned host dependency set, and Docker Compose for the servers.

## Verified runtime inventory (live on 2026-08-28)

Three Docker Compose projects + three launchd daemons. All ports loopback-only
(verified by `docker ps` and `curl`):

| Component | Where | Project / label | Status seen |
|---|---|---|---|
| Teammate Synapse (homeserver) | 127.0.0.1:**8008** | `matrix-wa` | up, healthy |
| Element Web (opt-in escape hatch UI) | 127.0.0.1:**8009** | `matrix-wa` (profile `escape`) | off by default — `docker compose --profile escape up -d element` |
| **New apps server** (`apps/` + `shared/`) | 127.0.0.1:**8011** | `matrix-wa` (`views`) | up — hardened headers via views/nginx.conf (2026-08-28) |
| 5 mautrix bridges (whatsapp, meta/IG, linkedin, twitter, gmessages) | compose network only, no host ports | `matrix-wa` | up |
| Postgres (teammate) | compose network only | `matrix-wa` | up, healthy |
| Master Synapse | 127.0.0.1:**8018** | `matrix-master` | up, healthy |
| Postgres (master) | compose network only | `matrix-master` | up, healthy |
| Enrollment/admin service (`master/enroll.py serve`) | 127.0.0.1:**8019** | launchd `com.jkali.master-enroll` | running |
| GMessages connect helper (`gmessages-connect/connect_server.py`) | 127.0.0.1:**8020** | launchd `com.jkali.gmessages-connect` | running — one-click Google Messages login for apps/user |
| iMessage bridge daemon (`imessage/daemon.py`) | 127.0.0.1:**29350** (appservice HTTP) | launchd `com.jkali.imessage-daemon` | running |
| Uplink daemon (`agents/uplink/uplink.py`) | no listening socket (outbound only) | launchd `com.jkali.uplink` | running, 6 mirrors, ~30s loop |
| Throwaway test Synapse | 127.0.0.1:**8028** | `matrix-synctest` | up 31h (should be down when not testing) |

Entry points:
- Teammate app: `http://127.0.0.1:8011/apps/user/index.html` (signs into :8008)
- Manager console: `http://127.0.0.1:8011/apps/master/index.html` (signs into :8018)

## The codebase, area by area (88 tracked files)

### `shared/` — the front-end core (ES modules, imported by both apps)
- `matrix/client.js` — fetch wrapper `api()`, bearer auth, `ROOMID_RE`/`MXC_RE`
  validation, `configureMatrixBase()` (used only by the master app to repoint
  at :8018). Per-page module instances keep the two apps isolated.
- `state.js` — the single mutable state object `S` + shared collections.
- `model/consent.js` — the **authorization boundary**: for conversations, a
  pure explicit-only resolver (per-conversation override `share` / `direct` /
  `private`; absent or unrecognized ⇒ private — no inheritance); contact
  sharing keeps its own per-source policy resolver. Plus normalization +
  account-data storage helpers. Verified in parity with the Python port (see
  Tests below).
- `model/contacts.js` — contact profiles (`com.jkali.contact_profiles`):
  one person across platforms; a room belongs to at most one profile;
  `normalizeProfiles()` re-validates on every read and write.
- `ui/el.js` — DOM builder (`el()`, textContent-only), sanitizers, `txn()`.
- `ui/render.js` — the render whitelist (`content.body` only, never
  `formatted_body`; media → static labels) and the from_me anti-spoof gate
  (`com.jkali.from_me` honored only when the sender is the iMessage bot).
- `ui/chat.js` — `openConvo`, the room-scoped live tail, and
  `sendConvoMessage()` — the guarded conversation-send path (re-validates the
  room at send time; refuses management rooms).
- `ui/sources.js` — the bridge table (`SOURCES`) + management-room
  resolution/verification + `sendCmd`/`sendSecretToMgmt` (the *command* send
  path into verified bot-management rooms) + the command sync loop.
- `ui/account-data.js`, `ui/search.js`, `ui/nav.js`, `ui/rows.js`,
  `ui/connections.js` — feed model, rendering, navigation, per-bridge cards.
  These eight `ui/` modules form **one cyclic import cluster** — importing any
  of them pulls in the send paths (this fact shapes the master app; F14).
- `style/organic.css`, `style/beepa.css` — the shared design system.

### `apps/user/` — the teammate app (on `shared/`)
`main.js` boots everything; `consent.js` (share controls + summary panel),
`contacts.js` (profiles UI), `proposals.js` (the manager-suggestion inbox —
approve calls `sendConvoMessage(target, body)`, the same guarded path),
`orglink.js` (Settings → "Connect to organization": redeems an enrollment code
against :8019 and writes the returned master credentials to the
`com.jkali.master_link` account-data, which the uplink reads). Audit found no
dead code here.

### `apps/master/` — the manager console (deliberately *not* on `shared/ui/`)
One file (`main.js`, 946 lines) + shell. Imports only the three send-free
leaves (`client.js`, `el.js`, `state.js`) and locally re-implements the small
read-side patterns (content whitelist, tail loop, badges) so that the send
path is *absent code*, not a hidden button. Verified: the only Matrix writes
are one `com.jkali.proposal` event into a discovered, allowlisted proposals
room, and a marker-scoped invite auto-join; plus one HTTPS-less loopback call
to the enroll admin endpoint (`/admin/add-teammate`). No `m.room.message` send
exists in the file (also statically asserted by integration scenario 7).

### `hub/` — the legacy monolith UI (retired)
Retired 2026-08-28 per `docs/SIMPLIFICATION-PLAN.md` P1: `hub/site/` was
deleted; `hub/nginx.conf` was kept as the donor for `views/nginx.conf`. The
teammate app on :8011 is the daily driver.

### `agents/uplink/` — the mirror-up daemon (Python stdlib, outbound-only)
`uplink.py` (987 lines): every ~30s loop — resolve effective consent for every
joined local room (`consent.py`, the Python port of the JS resolver), diff
against existing mirrors (`reconcile.py`), create/delete mirror rooms on the
master, forward new events (idempotent via a SQLite `event_map`), pull manager
proposals down into a dedicated local room (idempotent via `proposal_map`).
Mirror rooms are created with the manager pinned read-only (PL 0,
`events_default` 50) *in the createRoom call*; proposals rooms allow the
manager exactly one event type (`com.jkali.proposal`). Revocation = unlink
from the space + kick the manager + leave (CS-API clients can't purge).
State: `state.db` (600). Config: env or the `com.jkali.master_link`
account-data written by the user app. Known code-level issues: F3, F5–F9, F19.

### `master/` — the always-on master stack
`docker-compose.master.yml` (project `matrix-master`, Synapse on :8018,
Postgres with no host port), `setup.sh` (renders `homeserver.yaml`:
registration off, federation off, loosened rate limits; mints secrets, 600),
`provision.sh` (idempotent: `@manager`, `@alice`, `@bob` + one read-only
`space:<user>` per teammate; writes `tokens.local`), `enroll.py` (one-time
enrollment codes: mint/exchange/serve on :8019; codes stored SHA-256-only,
single-use, TTL'd; `/admin/add-teammate` provisions a new teammate end-to-end
after verifying the caller *is* the manager — note latent bug F4).

### `imessage/` — the iMessage bridge daemon (macOS-specific)
`daemon.py` (1,257 lines): a Matrix **appservice** on 127.0.0.1:29350 backed
by a pinned Swift CLI (`imessage/bin/imessage-cli`, from the vendored
`platform-imessage/` checkout) that reads the macOS Messages database. Two
HTTP endpoints only (`/health`, `/_matrix/app/v1/transactions/{txn}`); ghost
users per contact; sender-allowlisted, rate-capped, hash-only echo ledger.
Stamps `com.jkali.from_me` on user-authored messages (the flag the render gate
trusts only from this bot). Runtime invariants **unverified** (would require
sending real iMessages); code inspection found no dead endpoints.

### `gmessages-connect/` — the one-click Google Messages login helper
`connect.py`: the provisioning logic (reads Chrome cookies via Keychain,
drives the gmessages bridge's provisioning API) — usable standalone as a CLI.
`connect_server.py`: a loopback HTTP service (127.0.0.1:**8020**, launchd
`com.jkali.gmessages-connect`) that wraps `connect.py` so apps/user can drive
the login in **one click** (`Sign in & connect`). It reads cookies / calls the
bridge only inside an authorized `POST /connect/gmessages/start`; every POST is
gated on `Origin == http://127.0.0.1:8011` + `Content-Type: application/json` +
`X-Beepa-Connect: 1` before any side effect; `GET /connect/health` is
side-effect-free; cookies / the shared_secret / raw bridge bodies are never
returned or logged. See `gmessages-connect/CLAUDE.md`.

### `tests/`
- Unit: 19 unit tests (9 JS + 9 python — consent parity, uplink reconcile,
  auto-merge, proposals, contacts, and more), all wired into `tests/run.sh`.
- Integration: `harness.py` — **12** scenarios driving the real `uplink.py`
  against two live homeservers (the `matrix-synctest` throwaway on :8028 +
  the real master on :8018); `test_enroll.py` for the enrollment flow.
- `tests/run.sh` runs all 19 unit tests.

### Root docs
`PLAN*.md` / `SPEC-META-HUB.md` are historical design/decision records (13
files, one per feature wave), archived under `docs/history/` — superseded,
kept for reference only. `README.md` predated the master-sync layer
entirely and was rewritten as part of this audit.

## Data flow (verified against code)

```
WhatsApp/IG/LinkedIn/X/GMsg clouds        macOS Messages.db
        │ (mautrix bridges)                    │ (Swift CLI + daemon.py)
        ▼                                      ▼
   Teammate Synapse (127.0.0.1:8008)  ◄─ appservice on :29350
        │  ▲                                  
        │  └── apps/user (:8011) — read UI + THE guarded send path
        ▼
   agents/uplink (no socket; outbound only)
        │  reads consent (account-data) → mirrors ONLY shared rooms
        ▼
   Master Synapse (127.0.0.1:8018) — per-teammate space, read-only mirror rooms
        ▲                                   │
        │ apps/master (:8011) reads;        │ manager writes ONE event type
        │ cannot send                       ▼ (com.jkali.proposal)
        └── proposals room ──(uplink pulls down)──► local proposals room
                                                    → teammate reviews →
                                                    sendConvoMessage (their own account)
```

Authorization layers, inside-out (each independently verified in code):
1. Consent resolver (JS + Python, parity-tested) — default private.
2. Uplink mirrors only consent-shared rooms; revocation deletes the mirror.
3. Mirror-room power levels pin the manager to read-only at creation.
4. The master app contains no message-send code at all.
5. A proposal becomes a message only when the teammate sends it themselves
   through the same guarded path used for their own typing.

## How to run (verified commands)

```sh
# Teammate stack (bridges + homeserver + UIs):
docker compose --profile bridge --profile client up -d

# Master stack:
master/setup.sh                                    # render config + secrets
docker compose -p matrix-master --env-file master/.env \
  -f master/docker-compose.master.yml up -d
master/provision.sh                                # accounts + spaces + tokens.local

# Daemons (installed as launchd agents; verified loaded):
launchctl list | grep jkali    # com.jkali.imessage-daemon / uplink / master-enroll

# Unit tests (safe, no network — all verified passing 2026-08-28):
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/consent.test.js
python3 tests/unit/consent_py.test.py
python3 tests/unit/uplink_reconcile.test.py
python3 tests/unit/uplink_sources.test.py

# Integration tests (MUTATE the live master — do not run casually):
docker compose -p matrix-synctest -f tests/integration/docker-compose.test.yml up -d
tests/integration/run.sh
```

**Danger note (unchanged from before):** `docker compose down -v` on the
`matrix-wa` project destroys bridge sessions and history. `down -v` is safe
only for `matrix-master` and `matrix-synctest`.
