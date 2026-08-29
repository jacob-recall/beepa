#!/usr/bin/env bash
# setup.sh — one-command setup for a teammate's machine.
#
# Brings up the local hub stack and installs + loads the host "connect helpers"
# (launchd) so the one-click Connect buttons for Google Messages, Instagram,
# LinkedIn, and X work with NO terminal step per login. Safe to re-run: docker
# compose up is idempotent, and each launchd agent is unloaded before reload.
#
# This is the turn-on that used to be a manual `cp`/`launchctl load`. The helpers
# must run on the host (not in a container): they read the host's Chrome cookie
# store + Keychain and `docker compose exec` the bridges. Each binds 127.0.0.1
# only and reads a session solely inside an authorized, origin-gated request.
#
# Joining the manager's org is a SEPARATE, optional step (master-sync):
#   agents/uplink/link.sh <enroll-url> <code>
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log() { printf '[setup] %s\n' "$*" >&2; }

# --- 1. bring up the local stack (idempotent) ---
if command -v docker >/dev/null 2>&1; then
  log "starting the local hub (docker compose: bridge + client)…"
  ( cd "${HERE}" && docker compose --profile bridge --profile client up -d )
else
  log "docker not found — install Docker Desktop and re-run to start the stack."
fi

# --- 2. install + (re)load the host connect helpers (launchd) ---
LA_DIR="${HOME}/Library/LaunchAgents"
mkdir -p "${LA_DIR}"

# install_agent <plist_src> <label> <health_url>
install_agent() {
  local src="$1" label="$2" health="$3"
  local dest="${LA_DIR}/$(basename "${src}")"
  mkdir -p "$(dirname "${src}")/logs"
  if [ ! -f "${src}" ] || ! command -v launchctl >/dev/null 2>&1; then
    log "skip ${label}: plist or launchctl unavailable"
    return
  fi
  cp "${src}" "${dest}"
  launchctl unload "${dest}" 2>/dev/null || true
  if launchctl load "${dest}" 2>/dev/null; then
    log "loaded ${label}"
  else
    log "could not launchctl load ${label}; run: launchctl load '${dest}'"
    return
  fi
  # Best-effort liveness (health is side-effect-free — reads no cookies).
  sleep 1
  if command -v curl >/dev/null 2>&1 && curl -fsS "${health}" >/dev/null 2>&1; then
    log "  ${label} is up (${health})"
  else
    log "  ${label} not answering yet at ${health} — it may still be starting."
  fi
}

install_agent "${HERE}/gmessages-connect/com.jkali.gmessages-connect.plist" \
  "com.jkali.gmessages-connect" "http://127.0.0.1:8020/connect/health"
install_agent "${HERE}/session-connect/com.jkali.session-connect.plist" \
  "com.jkali.session-connect"   "http://127.0.0.1:8021/connect/health"

cat >&2 <<DONE

[setup] Done. One-click connect is on.
  - Open the app:  http://127.0.0.1:8011
  - Use the Connect buttons for WhatsApp / Google Messages / Instagram /
    LinkedIn / X — sign in once per network, no terminal, no paste.
  - Join the manager's org (optional): agents/uplink/link.sh <enroll-url> <code>
  - Stop a helper:  launchctl unload '${LA_DIR}/com.jkali.session-connect.plist'
DONE
