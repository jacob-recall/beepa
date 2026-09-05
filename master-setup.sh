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

# provision.sh adopts the complete existing roster; an explicit TEAMMATES
# value may add accounts, but a routine setup must not replace the roster.
python3 "${HERE}/install_config.py" --root "${HERE}" ensure --role master >/dev/null
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
BEEPA_PYTHON="$(python3 "${HERE}/install_config.py" --root "${HERE}" runtime)"
export BEEPA_PYTHON

# --------------------------------------------------------------------------
step "Preflight"
# If the docker CLI isn't on PATH but Docker Desktop IS installed, use its
# bundled CLI (avoids a pointless, failing `brew install --cask` when the app
# already exists but its CLI was never symlinked).
if ! command -v docker >/dev/null 2>&1 && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
  export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
fi
if ! command -v docker >/dev/null 2>&1; then
  if [ -d /Applications/Docker.app ]; then
    [ "$(uname -s 2>/dev/null)" = "Darwin" ] && open -a Docker 2>/dev/null || true
    fail "Docker Desktop is installed but its CLI isn't available. Launch it, wait for it to start, then re-run master-setup.sh."
  elif command -v brew >/dev/null 2>&1; then
    log "Docker not found — installing Docker Desktop via Homebrew…"
    HOMEBREW_NO_AUTO_UPDATE=1 brew install --cask docker || fail "brew install failed — install Docker Desktop manually: https://www.docker.com/products/docker-desktop/"
  else
    fail "Docker not found and Homebrew unavailable. Install Docker Desktop, then re-run: https://www.docker.com/products/docker-desktop/"
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
  # bind-test, not lsof: unprivileged lsof can't see a listener owned by another
  # user, so it would miss an occupied port. A bind attempt catches any owner.
  if ! python3 - "$p" 2>/dev/null <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1]))); s.close()
except OSError:
    sys.exit(1)
PY
  then
    log "port ${p} already in use — OK if that's this master stack from a previous run, otherwise free it"
  fi
done

# --------------------------------------------------------------------------
step "Step 1/6 — mint master/.env if absent"
ENV_FILE="${BEEPA_MASTER_STATE_DIR:-${BEEPA_INSTALL_ROOT}/master}/.env"
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
python3 "${HERE}/install_config.py" --root "${HERE}" compose --role master -- up -d
if python3 - "${BEEPA_MASTER_STATE_DIR:-${BEEPA_INSTALL_ROOT}/master}/.beepa-config/last-render.json" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
sys.exit(0 if p.exists() and "synapse/homeserver.yaml" in json.loads(p.read_text()).get("changed", []) else 1)
PY
then
  python3 "${HERE}/install_config.py" --root "${HERE}" compose --role master -- restart synapse
fi

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
"${HERE}/master/provision.sh"

# Passwordless auto-login for the manager console: mint a console-session token
# and write it where apps/master fetches it (apps/master/session.local.json,
# gitignored, 600). Best-effort — the console falls back to the login form.
MPW="$(python3 "${HERE}/master/enroll.py" password manager --manager 2>/dev/null || true)"
if [ -n "${MPW}" ]; then
  MTOK="$(python3 - "${MPW}" <<'PY'
import sys, json, urllib.request
pw = sys.argv[1]
body = json.dumps({"type":"m.login.password","identifier":{"type":"m.id.user","user":"manager"},
                   "password":pw,"initial_device_display_name":"beepa-master-console"}).encode()
try:
    print(json.load(urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:8018/_matrix/client/v3/login", data=body, headers={"Content-Type":"application/json"}))).get("access_token",""))
except Exception:
    pass
PY
)"
  if [ -n "${MTOK}" ]; then
    ( umask 077; printf '{"user_id":"@manager:master","access_token":"%s"}\n' "${MTOK}" > "${BEEPA_INSTALL_ROOT}/apps/master/session.local.json" )
    chmod 600 "${BEEPA_INSTALL_ROOT}/apps/master/session.local.json"
    log "passwordless login enabled for apps/master (no password screen)"
  fi
fi

# --------------------------------------------------------------------------
step "Step 6/6 — start the enrollment service (launchd) + expose over Tailscale"
LA_DIR="${HOME}/Library/LaunchAgents"; mkdir -p "${LA_DIR}" "${HERE}/master/logs"
PLIST_SRC="${HERE}/master/com.jkali.master-enroll.plist"
PLIST_DST="${LA_DIR}/com.jkali.master-enroll.plist"
if [ -f "${PLIST_SRC}" ] && command -v launchctl >/dev/null 2>&1; then
  python3 "${HERE}/install_config.py" --root "${HERE}" install-agent master-enroll
  log "loaded org.beepa.master-enroll"
  sleep 1
  curl -fsS http://127.0.0.1:8019/enroll/health >/dev/null 2>&1 \
    && log "enroll service answering on 127.0.0.1:8019" \
    || log "enroll service not answering yet (it may still be starting)"
else
  log "skip enroll service: plist or launchctl unavailable — run 'python3 master/enroll.py serve' manually"
fi

if command -v launchctl >/dev/null 2>&1; then
  python3 "${HERE}/install_config.py" --root "${HERE}" install-agent master-gateway
fi

if [ -x "${HERE}/master/tailscale-serve.sh" ]; then
  "${HERE}/master/tailscale-serve.sh" || log "Tailscale exposure skipped/failed (non-fatal) — see above"
else
  log "master/tailscale-serve.sh missing — master reachable on 127.0.0.1 only"
fi

# The web interface is shared with the teammate install, but can start without
# its database/config. Same Compose project/service avoids a second :8011 bind.
# Never remove orphans here: the teammate services may be running alongside it.
step "Install the master app and start its web interface"
python3 "${HERE}/install_config.py" --root "${HERE}" compose --role local-ui -- up -d views
if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
  python3 "${HERE}/desktop/install_apps.py" --role master
fi

# --------------------------------------------------------------------------
cat >&2 <<DONE

[master-setup] ============================================================
[master-setup] Master is set up.
  Manager app:      open Beepa Master.app in ~/Applications (macOS).
  Browser address:  http://127.0.0.1:8011/apps/master/index.html
  Manager login:    username 'manager', password from:
                      python3 master/enroll.py password manager --manager
  Onboard a teammate (them, or you on the hub side of this machine):
    1. python3 master/enroll.py mint <name>       # one-time code
    2. run ./install.sh on their machine; give it the enrollment URL
       (printed by tailscale-serve above) and the code.
[master-setup] ============================================================
DONE
