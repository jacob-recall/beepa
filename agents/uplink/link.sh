#!/usr/bin/env bash
# agents/uplink/link.sh <enroll-url> <code>
#
# One-command linker the new teammate runs on THEIR OWN install to join the
# org. It:
#   1. collects this install's LOCAL hub details (LOCAL_HS_URL / LOCAL_USER /
#      LOCAL_TOKEN) from env vars or an interactive prompt — the teammate
#      supplies their OWN local hub token here, never the master's;
#   2. redeems the one-time enrollment code via enroll_client.py, which returns
#      the MASTER_* scoped credentials (master base URL, the teammate's scoped
#      master token, manager mxid, master space);
#   3. writes agents/uplink/uplink.env.local (mode 600) combining LOCAL_* +
#      MASTER_*;
#   4. installs + loads the launchd uplink daemon.
#
# Safe to re-run. The enrollment code is single-use: if a prior run already
# consumed it, step 2 fails cleanly (the existing env file is left untouched)
# and the manager can issue a fresh code. Nothing here can send externally.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENROLL_URL="${1:-}"
CODE="${2:-}"

if [ -z "${ENROLL_URL}" ] || [ -z "${CODE}" ]; then
  cat >&2 <<'USAGE'
usage: agents/uplink/link.sh <enroll-url> <code>

  <enroll-url>  the master enrollment endpoint (e.g. https://master.example
                or, for a local test, http://127.0.0.1:8019)
  <code>        the one-time enrollment code the manager gave you

Local hub details are taken from the environment if set, else prompted for:
  LOCAL_HS_URL  your local homeserver base URL (e.g. http://127.0.0.1:8008)
  LOCAL_USER    your local mxid            (e.g. @you:localhost)
  LOCAL_TOKEN   your local access token    (kept only in this mode-600 file)
USAGE
  exit 2
fi

log() { printf '[link] %s\n' "$*" >&2; }
fail() { printf '[link] ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. collect LOCAL hub details (env or interactive prompt) ---
LOCAL_HS_URL="${LOCAL_HS_URL:-}"
LOCAL_USER="${LOCAL_USER:-}"
LOCAL_TOKEN="${LOCAL_TOKEN:-}"

if [ -z "${LOCAL_HS_URL}" ]; then
  printf 'Local homeserver URL [http://127.0.0.1:8008]: ' >&2
  read -r LOCAL_HS_URL || true
  LOCAL_HS_URL="${LOCAL_HS_URL:-http://127.0.0.1:8008}"
fi
if [ -z "${LOCAL_USER}" ]; then
  printf 'Local mxid (e.g. @you:localhost): ' >&2
  read -r LOCAL_USER || true
fi
if [ -z "${LOCAL_TOKEN}" ]; then
  printf 'Local access token (input hidden): ' >&2
  read -rs LOCAL_TOKEN || true
  printf '\n' >&2
fi

[ -n "${LOCAL_HS_URL}" ] || fail "LOCAL_HS_URL is required"
[ -n "${LOCAL_USER}" ]   || fail "LOCAL_USER is required"
[ -n "${LOCAL_TOKEN}" ]  || fail "LOCAL_TOKEN is required"

case "${LOCAL_USER}" in
  @*:*) : ;;
  *) fail "LOCAL_USER must be a full mxid like @you:localhost" ;;
esac

# --- 2. redeem the code -> MASTER_* scoped creds (to a temp file) ---
ENVFILE="${HERE}/uplink.env.local"
umask 077
MASTER_ENV="$(mktemp "${TMPDIR:-/tmp}/uplink.master.XXXXXX")"
trap 'rm -f "${MASTER_ENV}"' EXIT

log "redeeming enrollment code against ${ENROLL_URL} ..."
/usr/bin/python3 "${HERE}/enroll_client.py" \
  --enroll-url="${ENROLL_URL}" --code="${CODE}" --out="${MASTER_ENV}" \
  || fail "enrollment redemption failed (code may be used/expired); env file left unchanged"

# Pull the MASTER_* vars the exchange returned into this shell.
set -a
# shellcheck disable=SC1090
. "${MASTER_ENV}"
set +a

[ -n "${MASTER_HS_URL:-}" ] || fail "exchange did not return MASTER_HS_URL"
[ -n "${MASTER_USER:-}" ]   || fail "exchange did not return MASTER_USER"
[ -n "${MASTER_TOKEN:-}" ]  || fail "exchange did not return MASTER_TOKEN"
[ -n "${MASTER_SPACE:-}" ]  || fail "exchange did not return MASTER_SPACE"

# --- 3. write the combined uplink env file (mode 600), atomically ---
TMP="${ENVFILE}.tmp.$$"
{
  echo "# uplink env — mode 600, do NOT commit. Written by agents/uplink/link.sh."
  echo "# LOCAL_* = this install's own hub; MASTER_* = scoped master credentials."
  echo "LOCAL_HS_URL='${LOCAL_HS_URL}'"
  echo "LOCAL_USER='${LOCAL_USER}'"
  echo "LOCAL_TOKEN='${LOCAL_TOKEN}'"
  echo "MASTER_HS_URL='${MASTER_HS_URL}'"
  echo "MASTER_USER='${MASTER_USER}'"
  echo "MASTER_TOKEN='${MASTER_TOKEN}'"
  echo "MANAGER_MXID='${MANAGER_MXID:-}'"
  echo "MASTER_SPACE='${MASTER_SPACE}'"
} > "${TMP}"
chmod 600 "${TMP}"
mv "${TMP}" "${ENVFILE}"
chmod 600 "${ENVFILE}"
log "wrote ${ENVFILE} (mode 600)"

# --- 4. install + (re)load the launchd uplink daemon ---
PLIST_SRC="${HERE}/com.jkali.uplink.plist"
LA_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${LA_DIR}/com.jkali.uplink.plist"
if [ -f "${PLIST_SRC}" ] && command -v launchctl >/dev/null 2>&1; then
  mkdir -p "${LA_DIR}"
  # Rewrite the repo plist's placeholder paths to THIS checkout (same rule as
  # setup.sh install_agent) — a raw cp shipped /Users/jkali/... paths onto
  # other machines and the uplink never started.
  REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
  sed "s#/Users/jkali/work/pm_mng#${REPO_ROOT}#g" "${PLIST_SRC}" > "${PLIST_DEST}"
  launchctl unload "${PLIST_DEST}" 2>/dev/null || true
  if launchctl load "${PLIST_DEST}" 2>/dev/null; then
    log "loaded launchd daemon com.jkali.uplink"
  else
    log "could not launchctl load; start it manually: launchctl load '${PLIST_DEST}'"
  fi
else
  log "launchctl or plist unavailable; run the uplink manually: bash '${HERE}/run-uplink.sh'"
fi

cat >&2 <<DONE

[link] Done. Next steps:
  - The uplink daemon is installed and syncing your SHARED conversations up to
    the master. Nothing you have not shared ever leaves this machine.
  - Choose what to share in your teammate app (apps/user): per-conversation,
    per-source, or global Share-All.
  - Logs: ${HERE}/logs/uplink.log and uplink.err
  - To stop: launchctl unload '${PLIST_DEST}'
DONE
