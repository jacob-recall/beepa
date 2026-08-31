#!/usr/bin/env bash
# install.sh — guided teammate installer for onboarding steps 1-3:
#   1. stand up the local hub (Docker Compose: Synapse + mautrix bridges)
#   2. connect each network account (one-click Connect helpers)
#   3. start the uplink, enrolled to the central master
#
# This is a thin, ordered wrapper: it does NOT reimplement any of setup.sh,
# agents/uplink/link.sh, or master/enroll.py — it calls them. Safe to re-run
# (each step it drives is itself idempotent). No secrets are hardcoded here;
# everything sensitive is prompted for or read from existing mode-600 files.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log()  { printf '[install] %s\n' "$*" >&2; }
step() { printf '\n[install] === %s ===\n' "$*" >&2; }
fail() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

# Host ports this stack binds on the teammate's machine (loopback-only).
# See docker-compose.yml, session-connect/, gmessages-connect/, README.md.
HOST_PORTS=(8008 8009 8011 8020 8021 29350)

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
step "Preflight checks"

# Docker is required, but setup.sh (called in step 1) now installs it via
# Homebrew and starts it if needed — so don't hard-fail here, just note it.
if ! command -v docker >/dev/null 2>&1; then
  log "docker: not found — setup.sh will install Docker Desktop (Homebrew)."
elif ! docker info >/dev/null 2>&1; then
  log "docker: installed but not running — setup.sh will start it."
else
  log "docker: found and running"
fi

OS_NAME="$(uname -s 2>/dev/null || echo unknown)"
if [ "${OS_NAME}" != "Darwin" ]; then
  log "WARNING: this is not macOS. The iMessage bridge (imessage/daemon.py) only"
  log "         works on a Mac; every other network (WhatsApp, Google Messages,"
  log "         Instagram, LinkedIn, X) is unaffected."
else
  log "macOS: found (iMessage bridge is available)"
fi

PORT_BUSY=0
for p in "${HOST_PORTS[@]}"; do
  if command -v lsof >/dev/null 2>&1; then
    owner="$(lsof -nP -iTCP:"${p}" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1, $2}')"
  else
    owner=""
  fi
  if [ -n "${owner}" ]; then
    # A prior run of this same stack (docker-proxy / com.docker.backend, or
    # this repo's own launchd helpers) owning the port is expected and fine —
    # only warn, since re-running is meant to be safe.
    log "port ${p} is already in use (${owner}) — OK if that's this stack from a previous run"
    PORT_BUSY=1
  fi
done
[ "${PORT_BUSY}" = 0 ] && log "ports ${HOST_PORTS[*]}: all free"

# --------------------------------------------------------------------------
# Step 1 + 2 — bring up the stack, install the one-click Connect helpers
# --------------------------------------------------------------------------
# setup.sh already does both of these (docker compose up, then install +
# load the gmessages-connect / session-connect launchd helpers) — reuse it
# rather than duplicating its logic.
step "Step 1/3 — bring up the local hub (Docker Compose)"
step "Step 2/3 — install the one-click Connect helpers"
if [ -x "${HERE}/setup.sh" ]; then
  "${HERE}/setup.sh"
else
  fail "setup.sh not found or not executable at ${HERE}/setup.sh"
fi

log ""
log "Connect your accounts now (one click each, no terminal):"
log "  Open:  http://127.0.0.1:8011/apps/user/index.html"
log "  Use the Connect buttons for WhatsApp / Google Messages / Instagram /"
log "  LinkedIn / X. WhatsApp is QR-based (see README.md); the rest are"
log "  one-click via the helpers setup.sh just loaded."
if [ "${OS_NAME}" = "Darwin" ]; then
  log "  Contacts: setup.sh also loaded the hourly macOS Contacts importer;"
  log "  if macOS asked for Contacts access for osascript, Allow it. Your"
  log "  contacts stay on this Mac until you share them in the app."
fi

# --------------------------------------------------------------------------
# Step 3 — enroll the uplink to the central master
# --------------------------------------------------------------------------
step "Step 3/3 — enroll the uplink to the master"

# Use the LOCAL_* creds setup.sh's provisioning step wrote, so link.sh does not
# prompt for a hub token the teammate would have to find by hand.
LOCAL_ENV="${HERE}/agents/uplink/local.env.local"
if [ -f "${LOCAL_ENV}" ]; then
  set -a; . "${LOCAL_ENV}"; set +a
  log "local hub creds ready (${LOCAL_USER:-@jkali:localhost}) — link.sh will use them"
fi

UPLINK_ENV="${HERE}/agents/uplink/uplink.env.local"
if [ -f "${UPLINK_ENV}" ]; then
  log "already enrolled: ${UPLINK_ENV} exists."
  log "  To re-enroll (e.g. against a different master), remove that file and"
  log "  re-run this script, or run agents/uplink/link.sh directly."
else
  log "This step needs a one-time enrollment code from your manager"
  log "(minted via master/enroll.py mint <you>, or the manager console's"
  log "'add teammate' action)."

  ENROLL_URL="${MASTER_ENROLL_URL:-}"
  CODE="${MASTER_ENROLL_CODE:-}"

  if [ -t 0 ]; then
    if [ -z "${ENROLL_URL}" ]; then
      printf 'Master enrollment URL (e.g. https://master.example, blank to skip): ' >&2
      read -r ENROLL_URL || true
    fi
    if [ -n "${ENROLL_URL}" ] && [ -z "${CODE}" ]; then
      printf 'Enrollment code: ' >&2
      read -r CODE || true
    fi
  fi

  if [ -n "${ENROLL_URL}" ] && [ -n "${CODE}" ]; then
    log "enrolling via agents/uplink/link.sh ..."
    if "${HERE}/agents/uplink/link.sh" "${ENROLL_URL}" "${CODE}"; then
      log "uplink enrolled and running."
    else
      log "enrollment failed (code may be used/expired, or the master is unreachable)."
      log "  Retry manually when ready:"
      log "    agents/uplink/link.sh <enroll-url> <code>"
    fi
  else
    log "SKIPPED — no enrollment URL/code given. This is optional; run it later with:"
    log "    agents/uplink/link.sh <enroll-url> <code>"
  fi
fi

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------
cat >&2 <<DONE

[install] ============================================================
[install] Done.
  Teammate app:    http://127.0.0.1:8011/apps/user/index.html
  Connect status:  check the Connections card in the app for each network.
DONE
if [ -f "${UPLINK_ENV}" ]; then
  cat >&2 <<DONE
  Uplink:          enrolled — logs at agents/uplink/logs/uplink.log
  Verify on the master: ask your manager to confirm your rooms/space show up
                        in the manager console (apps/master).
DONE
else
  cat >&2 <<DONE
  Uplink:          NOT enrolled yet — run agents/uplink/link.sh <url> <code>
                    when you have a code from your manager.
DONE
fi
log "============================================================"
