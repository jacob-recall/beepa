<title>Installation Cost Scoping</title>
# Installation cost scoping: can teammates avoid Docker?

Written 2026-08-30 against the live stack (`docker-compose.yml`, `master/`,
`agents/uplink/`, `session-connect/`, `gmessages-connect/`). Grounded in what
this repo actually runs, not generic packaging advice.

## What's actually in the box today

Per `docker-compose.yml`, a teammate's local stack is 8 containers across 2
Compose projects (`matrix-wa` here, `matrix-master` separately for the
manager):

| Component | Image | What it is |
|---|---|---|
| `synapse` | `ghcr.io/element-hq/synapse:v1.159.0` | Matrix homeserver — **Python**, Twisted, 501MB image / 109MB compressed |
| `postgres` | `postgres:16.10-alpine` | DB for Synapse |
| `mautrix-whatsapp` | `dock.mau.dev/mautrix/whatsapp` | **Go**, single static binary, ~250MB image / 73MB compressed |
| `mautrix-meta` (Instagram) | `dock.mau.dev/mautrix/meta:ig-v26.08` | Go, same shape |
| `mautrix-linkedin` | `dock.mau.dev/mautrix/linkedin` | Go, same shape |
| `mautrix-twitter` | `dock.mau.dev/mautrix/twitter` | Go, same shape |
| `mautrix-gmessages` | `dock.mau.dev/mautrix/gmessages` | Go, same shape |
| `views` (nginx) | `nginx:1.29-alpine` | serves `apps/` + `shared/` static files |

Plus three things that **already run natively on the host, no Docker**,
because they must:
- `session-connect/` + `gmessages-connect/` — Python stdlib HTTP servers,
  launchd `KeepAlive` agents, because they need the host's Chrome cookie
  store + Keychain.
- `agents/uplink/uplink.py` — Python stdlib daemon, launchd agent, because it
  needs the host's local + master credentials.
- `imessage/daemon.py` — Python, launchd, because iMessage only exists on a
  Mac (`Messages.app`/`chat.db`).

**The load-bearing fact:** the mautrix bridges are *already* single Go
binaries — Docker buys them nothing but a consistent runtime and network
isolation. The only genuinely heavy, Python-and-Postgres-shaped piece is
**Synapse**. Everything else in the box is either already native-capable or
already runs natively.

## Path A — native binaries, no Docker

Run the mautrix bridges as plain host processes (they already build to
single binaries — `docker run`'s `--entrypoint` line in `master/setup.sh`
proves the image is just a binary + config) and swap the homeserver:

- **Option A1: Synapse natively.** Possible (`pip install matrix-synapse` +
  a venv), but this is the one piece that's genuinely Python + a real
  dependency tree (Twisted, cryptography, etc. — not stdlib), plus it still
  wants Postgres (SQLite works but isn't what's tested here). This barely
  reduces the install surface vs. Docker — you've traded "install Docker"
  for "install Python 3.11, a venv, Postgres.app or SQLite, and Synapse's
  own dependency set" while keeping the same moving parts. Not a strong path.
- **Option A2: swap the homeserver for a single Go/Rust binary** (Dendrite
  or Conduit/Conduwuit) that speaks the same Client-Server + Application
  Service APIs the mautrix bridges already use. This is the path that
  actually earns "no Docker, no Python for the server tier": every server
  component (bridges + homeserver) becomes a single native binary,
  supervised by launchd (macOS) / systemd (Linux) instead of Compose, with
  Postgres or SQLite as the only non-Go/Rust dependency.

Either way, orchestration moves from `docker compose up -d` /
`docker compose exec` to a set of launchd plists (the pattern already proven
by `session-connect`, `gmessages-connect`, and `agents/uplink` — three
services in this repo already run this way) plus a small supervisor script
in place of Compose's dependency ordering (`depends_on: condition:
service_healthy`) and log aggregation (`docker compose logs -f`).

**What changes vs. today:**
- `docker-compose.yml` → N launchd plists (one per bridge + homeserver +
  Postgres, or a bundled SQLite) + a `supervise.sh`/small Python supervisor
  for "wait for Postgres, then start Synapse, then start the bridges."
- `session-connect`/`gmessages-connect`'s `docker compose exec` calls into
  the bridge containers (for provisioning-API calls) become plain local
  HTTP calls to the bridge's own loopback port — arguably a simplification.
- Federation/appservice registration YAMLs (`*-registration.yaml`) are
  unaffected — they're homeserver-agnostic Application Service config, not
  Synapse-specific, *provided* the replacement homeserver implements the
  Application Service API (Dendrite and Conduit both do, to varying
  maturity — this needs a compatibility spike, not an assumption).
- `master/` (the always-on manager stack) is unaffected either way — it's
  hosted, not per-teammate, so its own Docker footprint doesn't gate
  teammate adoption.

## Path B — a downloadable, packaged install

Ship a signed `.app`/`.dmg` (or a `brew install` formula / `.pkg`) that
bundles Path A's native binaries and does first-run setup + service
registration for the user.

What it takes, concretely for this stack:
- Bundle the Synapse-replacement binary + the 5 mautrix Go binaries (all
  single-file, so bundling is `cp`, not a build system) inside the app
  bundle or a Homebrew formula's `bin/`.
- A launcher — either a menubar app (Swift/Electron/Tauri) that starts/stops
  the launchd agents and shows connect status, or a plain installer `.pkg`
  that drops the launchd plists + binaries and calls `launchctl load`
  (closer to what `setup.sh`/`install.sh` do today, just wrapped in a signed
  installer instead of a shell script the user runs by hand).
- **macOS code-signing + notarization** — required the moment this leaves a
  `git clone` + terminal audience: an unsigned `.app`/`.pkg` downloaded from
  the web gets Gatekeeper-blocked outright. Needs an Apple Developer Program
  enrollment, a Developer ID Application + Installer certificate, and
  `notarytool` in the release pipeline.
- **Auto-update** — Sparkle (for a menubar `.app`) or a formula-bump flow
  (for Homebrew) to ship bridge/Synapse-replacement version bumps without
  asking teammates to re-run an installer by hand.
- **Enrollment baked into first-run** — the installer's first-run flow
  absorbs step 3 (`agents/uplink/link.sh`): prompt for the master URL +
  code once, same `enroll_client.py` exchange, no separate terminal command.
  This part is cheap relative to signing/notarization — it's the same logic
  `install.sh` (this task's Part A) already wraps, just behind a GUI prompt
  instead of a shell prompt.

## Cost / effort assessment

| Path | Size | Why |
|---|---|---|
| **Keep Docker, script it** (this task's `install.sh`) | **Smallest** — hours | No architecture change. Wraps existing `setup.sh` + `agents/uplink/link.sh` + `master/enroll.py` in one guided, idempotent entry point with preflight checks. Ships today. |
| **Native binaries, no Docker** (Path A) | **Medium** — low-single-digit weeks | Main cost is the homeserver swap: proving Dendrite/Conduit's Application Service API is compatible with all 5 mautrix bridges' expectations (registration flow, transaction delivery, presence/typing if used), replacing Compose's health-check-gated startup ordering with a small supervisor, and re-testing every bridge login flow against the new homeserver. The bridges themselves need zero code changes — they're already the binaries this path ships. |
| **Fully packaged, signed app** (Path B) | **Largest** — several weeks to a couple months, plus ongoing cost | Path A's binaries are a prerequisite. On top: Apple Developer Program enrollment + cert management, a signing+notarization release pipeline, an installer or menubar-app UI layer, auto-update wiring, and the support burden of a "real" installed product (crash reports, upgrade failures, uninstall flow) instead of "re-clone the repo." |

## Hard constraints (true regardless of path)

- **iMessage requires a Mac.** `imessage/daemon.py` reads `chat.db` /
  `Messages.app` directly — there's no cross-platform substitute. Any
  Docker-free packaging still needs macOS for that one bridge; every other
  network (WhatsApp, Google Messages, Instagram, LinkedIn, X) doesn't care
  what OS the binaries run on.
- **The master must be hosted separately, always-on.** `master/` is its own
  Compose project (`matrix-master`) with its own Postgres — this doc's
  "avoid Docker" question is about the per-teammate install, not the
  manager's central stack, which is fine to keep on Docker (or a real
  server) since exactly one instance of it exists.

## Single highest-leverage move for adoption

**Ship `install.sh` today (Part A of this task) and treat that as the whole
near-term answer.** It removes the actual friction — "what do I run, in what
order, and did it work" — without touching the architecture. The Docker
requirement itself is not the adoption blocker for a technical early
audience; a five-step manual process with three separate launchd `cp`s is.
Native-binary packaging (Path A) only starts paying off once "download and
double-click" audience reach becomes the actual goal — and at that point the
real unlock is Path A's homeserver swap (it's the dependency Path B needs
anyway), not Docker removal for its own sake.
