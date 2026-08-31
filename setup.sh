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

# --- 0. ensure .env (Postgres password + host UID/GID) ---
# The compose stack needs POSTGRES_PASSWORD, and Synapse/bridges run as the host
# user so the bind-mounted config/state stays writable (the old hardcoded 501:20
# broke for any uid != 501). Both live in the gitignored .env. Mint it on first
# run; NEVER overwrite an existing one — that would change the DB password out
# from under an existing Postgres volume.
ENV_FILE="${HERE}/.env"
if [ ! -f "${ENV_FILE}" ]; then
  if command -v openssl >/dev/null 2>&1; then
    PW="$(openssl rand -hex 32)"
  else
    PW="$(head -c 48 /dev/urandom | LC_ALL=C tr -dc 'a-f0-9' | head -c 64)"
  fi
  ( umask 077; {
      printf 'POSTGRES_PASSWORD=%s\n' "${PW}"
      printf 'HOST_UID=%s\n' "$(id -u)"
      printf 'HOST_GID=%s\n' "$(id -g)"
    } > "${ENV_FILE}" )
  chmod 600 "${ENV_FILE}"
  log "created .env (fresh Postgres password; HOST_UID=$(id -u) HOST_GID=$(id -g))"
else
  # Existing .env: leave the password alone; just ensure the UID/GID lines exist
  # so the container user matches whoever owns the bind mounts.
  grep -q '^HOST_UID=' "${ENV_FILE}" || printf 'HOST_UID=%s\n' "$(id -u)" >> "${ENV_FILE}"
  grep -q '^HOST_GID=' "${ENV_FILE}" || printf 'HOST_GID=%s\n' "$(id -g)" >> "${ENV_FILE}"
  log ".env present — password unchanged; host UID/GID ensured"
fi

# --- 0b. render the hub config from tracked templates (idempotent) ---
# A fresh clone has no synapse/ or bridge config (they're gitignored). Render
# them from hub/templates/ with minted-or-reused secrets before starting Docker.
# Reuses synapse/.hub-secrets.local when present, so re-running never rotates
# tokens out from under a live stack.
if [ -x "${HERE}/hub/render-hub.sh" ]; then
  "${HERE}/hub/render-hub.sh"
else
  log "WARNING: hub/render-hub.sh missing — cannot render hub config"
fi

# --- 1. bring up the local stack (idempotent) ---
if command -v docker >/dev/null 2>&1; then
  log "starting the local hub (docker compose: bridge + client)…"
  ( cd "${HERE}" && docker compose --profile bridge --profile client up -d )
else
  log "docker not found — install Docker Desktop and re-run to start the stack."
fi

# --- 1b. provision this hub's local account + uplink LOCAL_TOKEN ---
# Fresh hub has no account (enable_registration:false); this registers one via
# the registration_shared_secret render-hub.sh added, and writes LOCAL_* for the
# uplink linker. Best-effort: on an already-configured hub it skips cleanly.
if [ -x "${HERE}/hub/provision-user.sh" ]; then
  "${HERE}/hub/provision-user.sh" || log "local-user provisioning skipped (non-fatal)"
fi

# --- 2. install + (re)load the host connect helpers (launchd) ---
LA_DIR="${HOME}/Library/LaunchAgents"
mkdir -p "${LA_DIR}"

# install_agent <plist_src> <label> [health_url]
# health_url is optional: a one-shot/timer job (no listener) passes "" and
# skips the liveness probe.
install_agent() {
  local src="$1" label="$2" health="${3:-}"
  local dest="${LA_DIR}/$(basename "${src}")"
  mkdir -p "$(dirname "${src}")/logs"
  if [ ! -f "${src}" ] || ! command -v launchctl >/dev/null 2>&1; then
    log "skip ${label}: plist or launchctl unavailable"
    return
  fi
  cp "${src}" "${dest}"
  # The tracked plists ship with the author's absolute path baked in; rewrite it
  # to THIS clone's path so the launchd service works wherever the teammate
  # cloned (otherwise every native daemon points at a nonexistent path and dies).
  sed -i '' "s#/Users/jkali/work/pm_mng#${HERE}#g" "${dest}" 2>/dev/null \
    || sed -i "s#/Users/jkali/work/pm_mng#${HERE}#g" "${dest}"
  launchctl unload "${dest}" 2>/dev/null || true
  if launchctl load "${dest}" 2>/dev/null; then
    log "loaded ${label}"
  else
    log "could not launchctl load ${label}; run: launchctl load '${dest}'"
    return
  fi
  # Best-effort liveness (health is side-effect-free — reads no cookies).
  [ -n "${health}" ] || return 0
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

# macOS Contacts importer (agents/contacts/): hourly one-shot launchd job that
# reads Contacts.app into the local, mode-600 contacts.db. Nothing leaves the
# machine until the app's contact-share panel says so (default: private).
# The FIRST run triggers the standard macOS "osascript wants access to your
# Contacts" prompt — that prompt is the consent surface; click Allow. A
# background launchd run can't answer it, so if it was missed/denied, enable
# osascript under System Settings > Privacy & Security > Contacts and run
# `python3 agents/contacts/import_macos.py` once by hand.
if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
  log "macOS may now ask for Contacts access for 'osascript' — click Allow."
  install_agent "${HERE}/agents/contacts/com.jkali.contacts-import.plist" \
    "com.jkali.contacts-import" ""

  # iMessage appservice daemon (imessage/daemon.py): a KeepAlive launchd
  # agent, macOS only. It needs two things this script does NOT create —
  # its secrets file and the pinned Beeper CLI build — so it is loaded only
  # when both exist; otherwise say exactly what is missing and carry on
  # (every other network is unaffected).
  IMSG_CFG="${HERE}/imessage/daemon.json"
  IMSG_CLI="${HERE}/imessage/bin/imessage-cli"
  if [ -f "${IMSG_CFG}" ] && [ -x "${IMSG_CLI}" ]; then
    install_agent "${HERE}/imessage/com.jkali.imessage-daemon.plist" \
      "com.jkali.imessage-daemon" ""
  else
    log "skip com.jkali.imessage-daemon — prerequisites missing:"
    [ -f "${IMSG_CFG}" ] || log "  - ${IMSG_CFG}: copy imessage/daemon.json.example and fill as_token/hs_token" \
      "from synapse/imessage-registration.yaml, user_id, self_handle, cli_path (chmod 600)"
    [ -x "${IMSG_CLI}" ] || log "  - ${IMSG_CLI}: the pinned platform-imessage CLI build (see README.md 'iMessage bridge')"
    log "  then re-run setup.sh to load it."
  fi
fi

cat >&2 <<DONE

[setup] Done. One-click connect is on.
  - Open the app:  http://127.0.0.1:8011
  - Use the Connect buttons for WhatsApp / Google Messages / Instagram /
    LinkedIn / X — sign in once per network, no terminal, no paste.
  - Join the manager's org (optional): agents/uplink/link.sh <enroll-url> <code>
  - Contacts (macOS): imported hourly into agents/contacts/contacts.db; share
    them from the app's contact-share panel (default: private).
  - iMessage (macOS): com.jkali.imessage-daemon is loaded when
    imessage/daemon.json + imessage/bin/imessage-cli exist (see above).
  - Stop a helper:  launchctl unload '${LA_DIR}/com.jkali.session-connect.plist'
DONE
