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

# --- preflight: ensure Docker is installed AND running ---
# Installs Docker Desktop via Homebrew if it is missing, then launches it and
# waits. Docker Desktop's first launch is interactive (license/privileged
# helper), so if it can't come up we stop with clear guidance rather than fail
# opaquely later.
ensure_docker() {
  # If the docker CLI isn't on PATH but Docker Desktop IS installed, use its
  # bundled CLI (common: the app exists but its CLI was never symlinked to
  # /usr/local/bin) — this avoids a pointless, failing `brew install --cask`.
  if ! command -v docker >/dev/null 2>&1 && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
    export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
  fi
  if ! command -v docker >/dev/null 2>&1; then
    if [ -d /Applications/Docker.app ]; then
      log "Docker Desktop is installed but its CLI isn't available. Launching it —"
      log "accept any prompt, wait for it to start, then re-run setup.sh."
      [ "$(uname -s 2>/dev/null)" = "Darwin" ] && open -a Docker 2>/dev/null || true
      exit 1
    elif command -v brew >/dev/null 2>&1; then
      log "Docker not found — installing Docker Desktop via Homebrew…"
      HOMEBREW_NO_AUTO_UPDATE=1 brew install --cask docker || { log "brew install failed — install Docker Desktop manually then re-run: https://www.docker.com/products/docker-desktop/"; exit 1; }
    else
      log "Docker not found and Homebrew unavailable. Install Docker Desktop, then re-run:"
      log "  https://www.docker.com/products/docker-desktop/"
      exit 1
    fi
  fi
  if ! docker info >/dev/null 2>&1; then
    log "Docker is installed but not running — launching Docker Desktop…"
    [ "$(uname -s 2>/dev/null)" = "Darwin" ] && open -a Docker 2>/dev/null || true
    log "waiting for Docker to start (up to ~90s)…"
    for _ in $(seq 1 45); do docker info >/dev/null 2>&1 && break; sleep 2; done
    docker info >/dev/null 2>&1 || { log "Docker still not running. Start Docker Desktop, then re-run setup.sh."; exit 1; }
  fi
  log "docker: installed and running"
}
ensure_docker

# --- 0. ensure .env (Postgres password + host UID/GID) ---
# The compose stack needs POSTGRES_PASSWORD, and Synapse/bridges run as the host
# user so the bind-mounted config/state stays writable (the old hardcoded 501:20
# broke for any uid != 501). Both live in the gitignored .env. Mint it on first
# run; NEVER overwrite an existing one — that would change the DB password out
# from under an existing Postgres volume.
ENV_FILE="${HERE}/.env"

# Adopt the existing identity or persist the first-install OS/supplied value.
# Output is shell-quoted by our own parser; no existing env file is executed.
identity_env="$(python3 "${HERE}/install_config.py" --root "${HERE}" ensure --role teammate --shell)"
eval "${identity_env}"
log "identity: @${LOCAL_LOCALPART}:localhost (persisted installation identity)"
export LOCAL_LOCALPART LOCAL_DISPLAYNAME
BEEPA_INSTALL_ROOT="$(python3 "${HERE}/install_config.py" --root "${HERE}" initialize-state)"
export BEEPA_INSTALL_ROOT
BEEPA_MASTER_STATE_DIR="$(python3 - "${HERE}/.beepa-install.json" <<'PYCODE'
import json, sys
from pathlib import Path
data = json.load(open(sys.argv[1]))
print(data.get('master_state_root', str(Path(data['state_root']) / 'master')))
PYCODE
)"
export BEEPA_MASTER_STATE_DIR
ENV_FILE="${BEEPA_INSTALL_ROOT}/.env"
BEEPA_PYTHON="$(python3 "${HERE}/install_config.py" --root "${HERE}" runtime)"
export BEEPA_PYTHON

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
      printf 'LOCAL_LOCALPART=%s\n' "${LOCAL_LOCALPART}"
      printf 'LOCAL_DISPLAYNAME=%s\n' "${LOCAL_DISPLAYNAME}"
    } > "${ENV_FILE}" )
  chmod 600 "${ENV_FILE}"
  log "created .env (fresh Postgres password; HOST_UID=$(id -u) HOST_GID=$(id -g); identity @${LOCAL_LOCALPART}:localhost)"
else
  # Existing .env: leave the password AND the identity alone; just ensure the
  # UID/GID + identity lines exist (append if a pre-identity .env is missing them).
  grep -q '^HOST_UID=' "${ENV_FILE}" || printf 'HOST_UID=%s\n' "$(id -u)" >> "${ENV_FILE}"
  grep -q '^HOST_GID=' "${ENV_FILE}" || printf 'HOST_GID=%s\n' "$(id -g)" >> "${ENV_FILE}"
  grep -q '^LOCAL_LOCALPART=' "${ENV_FILE}" || printf 'LOCAL_LOCALPART=%s\n' "${LOCAL_LOCALPART}" >> "${ENV_FILE}"
  grep -q '^LOCAL_DISPLAYNAME=' "${ENV_FILE}" || printf 'LOCAL_DISPLAYNAME=%s\n' "${LOCAL_DISPLAYNAME}" >> "${ENV_FILE}"
  log ".env present — password + identity unchanged; host UID/GID ensured"
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
  python3 "${HERE}/install_config.py" --root "${HERE}" compose -- up -d
  # Bind-mounted YAML changes are not a Compose service-definition change.
  # Explicitly restart only services whose effective configuration changed.
  changed_services="$(python3 - "${HERE}" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from beepa_update import changed_config_services
path = Path(sys.argv[1]) / '.beepa-config/last-render.json'
if path.exists():
    print(' '.join(changed_config_services(json.loads(path.read_text()).get('changed', []))))
PY
)"
  if [ -n "${changed_services}" ]; then
    # Values are the fixed service allowlist from changed_config_services.
    python3 "${HERE}/install_config.py" --root "${HERE}" compose -- restart ${changed_services}
  fi
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
  name="${label##*.}"
  if python3 "${HERE}/install_config.py" --root "${HERE}" install-agent "${name}"; then
    log "loaded org.beepa.${name}"
  else
    log "could not install org.beepa.${name}; rerun setup after resolving launchctl error"
    return 1
  fi
  # Best-effort liveness (health is side-effect-free — reads no cookies).
  [ -n "${health}" ] || return 0
  sleep 1
  if command -v curl >/dev/null 2>&1 && curl -fsS "${health}" >/dev/null 2>&1; then
    log "  ${label} is up (${health})"
    return
  fi
  # The helpers auto-fall back to the next port when theirs is taken (e.g.
  # 8021 held by another process -> 8022; CSP already allows the range, and
  # the app auto-discovers via connect.local.json). Probe the fallbacks
  # before declaring it down — "not answering at 8021" used to read as a
  # failure when the service was healthy on 8022.
  port="${health##*:}"; port="${port%%/*}"
  for off in 1 2 3 4; do
    alt="${health/:${port}\//:$((port + off))/}"
    if command -v curl >/dev/null 2>&1 && curl -fsS "${alt}" >/dev/null 2>&1; then
      log "  ${label} is up on fallback port $((port + off)) (${alt}) — its usual port ${port} was busy; this is fine."
      return
    fi
  done
  log "  ${label} not answering yet at ${health} — it may still be starting."
}

install_agent "${HERE}/gmessages-connect/com.jkali.gmessages-connect.plist" \
  "com.jkali.gmessages-connect" "http://127.0.0.1:8020/connect/health"
install_agent "${HERE}/session-connect/com.jkali.session-connect.plist" \
  "com.jkali.session-connect"   "http://127.0.0.1:8021/connect/health"

# Uplink daemon (agents/uplink/): install it now so it runs IDLE and begins
# mirroring the moment you enroll from the app (Settings > Connect to
# organization) — no terminal step. It polls the local hub's account-data for
# com.jkali.master_link and idles (logs "not connected", sleeps) until it
# appears. Needs the LOCAL_* creds provision-user.sh just wrote; skip if neither
# creds file exists yet so KeepAlive can't crash-loop a credential-less daemon.
if [ -f "${HERE}/agents/uplink/local.env.local" ] || [ -f "${HERE}/agents/uplink/uplink.env.local" ]; then
  install_agent "${HERE}/agents/uplink/com.jkali.uplink.plist" "com.jkali.uplink" ""
else
  log "skip com.jkali.uplink — no hub creds yet; it loads on a re-run after provisioning succeeds."
fi

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

  # iMessage appservice daemon (imessage/daemon.py): a KeepAlive launchd agent,
  # macOS only. daemon.json is rendered by hub/render-hub.sh (tokens match the
  # registration; self_handle left as a placeholder). The signed CLI is downloaded on demand
  # from Beeper's pinned public release. It loads only when the binary exists AND the
  # teammate has filled in self_handle — otherwise say what's missing and carry
  # on (every other network is unaffected). Set SKIP_IMESSAGE=1 to opt out.
  # The pinned Developer-ID-signed CLI is downloaded, not compiled. Preserve
  # the existing executable and its macOS permission identity on every rerun.
  IMSG_CFG="${BEEPA_INSTALL_ROOT}/imessage/daemon.json"
  IMSG_CLI="$(python3 - "${BEEPA_INSTALL_ROOT}/.beepa-install.json" <<'PYCODE'
import json, sys
print(json.load(open(sys.argv[1]))['imessage_cli_path'])
PYCODE
)"
  if [ ! -x "${IMSG_CLI}" ]; then
    [ -x "${HERE}/imessage/build-cli.sh" ] && "${HERE}/imessage/build-cli.sh" || true
  fi
  IMSG_HANDLE_UNSET=0
  grep -q 'REPLACE_WITH_YOUR_IMESSAGE_HANDLE' "${IMSG_CFG}" 2>/dev/null && IMSG_HANDLE_UNSET=1
  if [ -f "${IMSG_CFG}" ] && [ -x "${IMSG_CLI}" ] && [ "${IMSG_HANDLE_UNSET}" = 0 ]; then
    install_agent "${HERE}/imessage/com.jkali.imessage-daemon.plist" \
      "com.jkali.imessage-daemon" ""
  else
    log "skip com.jkali.imessage-daemon — not ready yet:"
    [ -f "${IMSG_CFG}" ] || log "  - ${IMSG_CFG} missing (hub/render-hub.sh renders it)"
    [ -x "${IMSG_CLI}" ] || log "  - ${IMSG_CLI} missing — the pinned signed download did not install; inspect build-cli.sh output above for download, checksum, or Developer ID signature failures, then retry. No Swift compilation is required."
    [ "${IMSG_HANDLE_UNSET}" = 1 ] && log "  - set self_handle (your iMessage phone/email) in ${IMSG_CFG}, and grant the CLI Full Disk Access"
    log "  then re-run setup.sh to load it."
  fi
fi

if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
  python3 "${HERE}/desktop/install_apps.py" --role user
fi

cat >&2 <<DONE

[setup] Done. One-click connect is on.
  - Open Beepa.app in ~/Applications (macOS); drag it to the Dock if wanted.
  - Browser address: http://127.0.0.1:8011/apps/user/index.html
  - Use the Connect buttons for WhatsApp / Google Messages / Instagram /
    LinkedIn / X — sign in once per network, no terminal, no paste.
  - Join the manager's org (optional): agents/uplink/link.sh <enroll-url> <code>
  - Contacts (macOS): imported hourly into agents/contacts/contacts.db; share
    them from the app's contact-share panel (default: private).
  - iMessage (macOS): org.beepa.imessage-daemon is loaded when
    imessage/daemon.json + imessage/bin/imessage-cli exist (see above).
  - Stop a helper:  launchctl unload '${LA_DIR}/org.beepa.session-connect.plist'
DONE
