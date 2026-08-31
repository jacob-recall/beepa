#!/usr/bin/env bash
# master-setup.sh — one command to stand up the always-on MASTER on THIS machine.
#
# Parallels install.sh (which stands up a teammate hub). This brings the
# matrix-master stack up, provisions the manager + teammate accounts/spaces,
# starts the enrollment service, and exposes the master over Tailscale so
# teammates' uplinks can reach it. Safe to re-run (every step is idempotent).
#
# It does NOT reimplement master/setup.sh, provision.sh, or tailscale-serve.sh —
# it orchestrates them in order. Nothing sensitive is hardcoded.
#
# After this: mint a code (python3 master/enroll.py mint <name>) and run
# install.sh on the hub side (this same machine is fine) to enroll.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
log()  { printf '[master-setup] %s\n' "$*" >&2; }
step() { printf '\n[master-setup] === %s ===\n' "$*" >&2; }
fail() { printf '[master-setup] ERROR: %s\n' "$*" >&2; exit 1; }

# Real teammate roster (space-separated). Defaults to the local user so a
# single-machine dogfood works; add more names or set TEAMMATES=... to include
# others up front (provision.sh is idempotent and also grows via the console).
TEAMMATES="${TEAMMATES:-jkali}"

# --------------------------------------------------------------------------
step "Preflight"
# Install Docker Desktop via Homebrew if missing, then launch + wait for it.
if ! command -v docker >/dev/null 2>&1; then
  log "Docker not found."
  if command -v brew >/dev/null 2>&1; then
    log "installing Docker Desktop via Homebrew…"
    brew install --cask docker || fail "brew install failed — install Docker Desktop manually: https://www.docker.com/products/docker-desktop/"
  else
    fail "Homebrew not found. Install Docker Desktop, then re-run: https://www.docker.com/products/docker-desktop/"
  fi
fi
if ! docker info >/dev/null 2>&1; then
  log "Docker installed but not running — launching Docker Desktop…"
  [ "$(uname -s 2>/dev/null)" = "Darwin" ] && open -a Docker 2>/dev/null || true
  log "waiting for Docker (up to ~90s)…"
  for _ in $(seq 1 45); do docker info >/dev/null 2>&1 && break; sleep 2; done
  docker info >/dev/null 2>&1 || fail "Docker still not running. Start Docker Desktop, then re-run master-setup.sh."
fi
log "docker: installed and running"
for p in 8018 8019; do
  if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${p}" -sTCP:LISTEN >/dev/null 2>&1; then
    log "port ${p} already in use — OK if that's this master stack from a previous run"
  fi
done

# --------------------------------------------------------------------------
step "Step 1/6 — mint master/.env if absent"
ENV_FILE="${HERE}/master/.env"
if [ ! -f "${ENV_FILE}" ]; then
  if command -v openssl >/dev/null 2>&1; then PW="$(openssl rand -hex 32)"
  else PW="$(head -c 48 /dev/urandom | LC_ALL=C tr -dc 'a-f0-9' | head -c 64)"; fi
  ( umask 077; {
      printf 'MASTER_POSTGRES_PASSWORD=%s\n' "${PW}"
      printf 'HOST_UID=%s\n' "$(id -u)"
      printf 'HOST_GID=%s\n' "$(id -g)"
    } > "${ENV_FILE}" )
  chmod 600 "${ENV_FILE}"
  log "created master/.env (fresh Postgres password; HOST_UID=$(id -u) HOST_GID=$(id -g))"
else
  grep -q '^HOST_UID=' "${ENV_FILE}" || printf 'HOST_UID=%s\n' "$(id -u)" >> "${ENV_FILE}"
  grep -q '^HOST_GID=' "${ENV_FILE}" || printf 'HOST_GID=%s\n' "$(id -g)" >> "${ENV_FILE}"
  log "master/.env present — password unchanged; host UID/GID ensured"
fi

# --------------------------------------------------------------------------
step "Step 2/6 — render master Synapse config + mint secrets"
"${HERE}/master/setup.sh"

# --------------------------------------------------------------------------
step "Step 3/6 — bring the matrix-master stack up"
docker compose -p matrix-master --env-file "${ENV_FILE}" \
  -f "${HERE}/master/docker-compose.master.yml" up -d

step "Step 4/6 — wait for the master Synapse to be healthy"
up=0
for _ in $(seq 1 40); do
  curl -fsS http://127.0.0.1:8018/health >/dev/null 2>&1 && { up=1; break; }
  sleep 2
done
[ "${up}" = 1 ] || fail "master Synapse did not come healthy on 127.0.0.1:8018 (check: docker compose -p matrix-master logs synapse)"
log "master Synapse is up (127.0.0.1:8018)"

# --------------------------------------------------------------------------
step "Step 5/6 — provision manager + teammate accounts and spaces"
TEAMMATES="${TEAMMATES}" "${HERE}/master/provision.sh"

# --------------------------------------------------------------------------
step "Step 6/6 — start the enrollment service (launchd) + expose over Tailscale"
LA_DIR="${HOME}/Library/LaunchAgents"; mkdir -p "${LA_DIR}" "${HERE}/master/logs"
PLIST_SRC="${HERE}/master/com.jkali.master-enroll.plist"
PLIST_DST="${LA_DIR}/com.jkali.master-enroll.plist"
if [ -f "${PLIST_SRC}" ] && command -v launchctl >/dev/null 2>&1; then
  cp "${PLIST_SRC}" "${PLIST_DST}"
  # Rewrite the author's baked-in absolute path to THIS clone's path so the
  # launchd service starts wherever the master was cloned.
  sed -i '' "s#/Users/jkali/work/pm_mng#${HERE}#g" "${PLIST_DST}" 2>/dev/null \
    || sed -i "s#/Users/jkali/work/pm_mng#${HERE}#g" "${PLIST_DST}"
  launchctl unload "${PLIST_DST}" 2>/dev/null || true
  if launchctl load "${PLIST_DST}" 2>/dev/null; then log "loaded com.jkali.master-enroll"
  else log "could not launchctl load; run: launchctl load '${PLIST_DST}'"; fi
  sleep 1
  curl -fsS http://127.0.0.1:8019/enroll/health >/dev/null 2>&1 \
    && log "enroll service answering on 127.0.0.1:8019" \
    || log "enroll service not answering yet (it may still be starting)"
else
  log "skip enroll service: plist or launchctl unavailable — run 'python3 master/enroll.py serve' manually"
fi

if [ -x "${HERE}/master/tailscale-serve.sh" ]; then
  "${HERE}/master/tailscale-serve.sh" || log "Tailscale exposure skipped/failed (non-fatal) — see above"
else
  log "master/tailscale-serve.sh missing — master reachable on 127.0.0.1 only"
fi

# --------------------------------------------------------------------------
cat >&2 <<DONE

[master-setup] ============================================================
[master-setup] Master is set up.
  Manager console:  open apps/master/index.html against 127.0.0.1:8018
  Manager login:    username 'manager', password from:
                      python3 master/enroll.py password manager --manager
  Onboard a teammate (them, or you on the hub side of this machine):
    1. python3 master/enroll.py mint <name>       # one-time code
    2. run ./install.sh on their machine; give it the enrollment URL
       (printed by tailscale-serve above) and the code.
[master-setup] ============================================================
DONE
